"""Connection factory and type hydration."""

import json
import pathlib
import sqlite3
from datetime import datetime, timezone

from .errors import PersistenceUnavailable


def hydrating_row_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    """A row factory that hydrates types at the connection seam."""
    d = {}
    for idx, col in enumerate(cursor.description):
        name = col[0]
        val = row[idx]
        
        if val is None:
            d[name] = None
            continue
            
        if name in ("is_verified", "is_class_3", "is_clean_process", "is_labeled", "are_photos_uploaded", "xray_images_done"):
            d[name] = bool(val)
        elif name.endswith("_at"):
            # SQLite stores dates as ISO 8601 strings
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d[name] = dt
        elif name == "traveler_metadata":
            d[name] = json.loads(val)
        elif name == "local_storage_path":
            d[name] = pathlib.Path(val)
        else:
            d[name] = val
            
    return d


import contextlib
import logging
import os
import re
import sys
import threading
from enum import Enum
from typing import Iterator, Optional

from .errors import ScopeConflict, UnexpectedConnectionThread, WriteRefused

logger = logging.getLogger(__name__)


# ---------- Patch 14 §8: one gate in front of every statement (INV-D1) ----------

class AccessMode(Enum):
    STRICT = "strict"
    REPORT = "report"


class StatementKind(Enum):
    READ = "read"
    WRITE = "write"


_LEADING_NOISE = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*.*?\*/", re.S)
_FIRST_WORD = re.compile(r"[A-Za-z_]+")
_DML_WORD = re.compile(r"\b(?:INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
_WRITING_PRAGMA = re.compile(r"\b(?:wal_checkpoint|optimize|incremental_vacuum|shrink_memory)\b", re.I)


def _strip_leading_noise(statement: str) -> str:
    position = 0
    while True:
        match = _LEADING_NOISE.match(statement, position)
        if match is None or match.end() == position:
            return statement[position:]
        position = match.end()


def statement_verb(statement: str) -> str:
    """The first keyword of a statement, upper-cased, after leading whitespace and comments; "EMPTY" if none."""
    match = _FIRST_WORD.match(_strip_leading_noise(statement))
    return match.group(0).upper() if match else "EMPTY"


def statement_kind(statement: str) -> StatementKind:
    """Whether a statement can change the database or its transaction state (Patch 14 §8.2).

    post: READ iff the verb is SELECT, EXPLAIN or EMPTY; or WITH with no INSERT, UPDATE, DELETE or REPLACE
          as a word; or PRAGMA with no "=" naming none of wal_checkpoint, optimize, incremental_vacuum or
          shrink_memory. WRITE otherwise, BEGIN, SAVEPOINT, RELEASE, ROLLBACK and COMMIT included
    """
    verb = statement_verb(statement)
    if verb in ("SELECT", "EXPLAIN", "EMPTY"):
        return StatementKind.READ
    text = _strip_leading_noise(statement)
    if verb == "WITH":
        return StatementKind.WRITE if _DML_WORD.search(text) else StatementKind.READ
    if verb == "PRAGMA":
        return StatementKind.WRITE if ("=" in text or _WRITING_PRAGMA.search(text)) else StatementKind.READ
    return StatementKind.WRITE


def _caller_outside_this_module() -> str:
    frame = sys._getframe(1)
    while frame is not None and frame.f_globals.get("__name__") == __name__:
        frame = frame.f_back
    if frame is None:
        return "unknown"
    return f"{frame.f_globals.get('__name__', '?')}:{frame.f_code.co_name}:{frame.f_lineno}"


class _Gate:
    """The state behind GatedConnection.admit: the owner, the worker scope, and the counts."""

    def __init__(self, owner: int) -> None:
        self.owner = owner
        self.mode = AccessMode.REPORT
        self.access_log: Optional[str] = None
        self.scope_ident: Optional[int] = None
        self.scope_holder: Optional[str] = None
        self.scope_depth = 0
        self.refused = 0
        self.off_owner = 0
        self._reported: set[tuple[int, str]] = set()
        self._lock = threading.Lock()

    def admit(self, statement: Optional[str] = None, verb: Optional[str] = None) -> None:
        """Lets the statement through, or raises (Patch 14 §8.1). Normal use costs one ident comparison."""
        caller = threading.get_ident()
        scope = self.scope_ident
        if scope is not None and caller == scope:
            return
        if scope is not None:
            kind = StatementKind.WRITE if statement is None else statement_kind(statement)
            if kind is StatementKind.WRITE:
                self._refuse(verb or statement_verb(statement), caller)
        if caller != self.owner:
            self._off_owner(verb or statement_verb(statement or ""), caller)

    def _refuse(self, verb: str, caller: int) -> None:
        with self._lock:
            self.refused += 1
            holder = self.scope_holder or "a worker"
        logger.warning(
            "Write refused: %s from thread %s while %s holds the connection",
            verb, threading.current_thread().name, holder,
        )
        raise WriteRefused(verb, holder)

    def _off_owner(self, verb: str, caller: int) -> None:
        name = threading.current_thread().name
        with self._lock:
            self.off_owner += 1
            first = (caller, name) not in self._reported
            if first:
                self._reported.add((caller, name))
        if first:
            where = _caller_outside_this_module()
            logger.warning("Connection used from thread %s (%s), which doesn't own it, at %s", name, verb, where)
            if self.access_log:
                try:
                    with open(self.access_log, "a", encoding="utf-8") as handle:
                        handle.write(f"thread={name} ident={caller} verb={verb} at={where}\n")
                except OSError:
                    pass
        if self.mode is AccessMode.STRICT:
            raise UnexpectedConnectionThread(name, verb)

    def enter_scope(self, holder: str) -> None:
        caller = threading.get_ident()
        with self._lock:
            if self.scope_ident is None:
                self.scope_ident, self.scope_holder, self.scope_depth = caller, holder, 1
            elif self.scope_ident == caller:
                self.scope_depth += 1
            else:
                raise ScopeConflict(self.scope_holder or "a worker")

    def exit_scope(self) -> None:
        with self._lock:
            self.scope_depth -= 1
            if self.scope_depth <= 0:
                self.scope_ident, self.scope_holder, self.scope_depth = None, None, 0


class GatedCursor(sqlite3.Cursor):
    """A cursor of a GatedConnection: every statement passes the connection's gate first."""

    def execute(self, sql, parameters=(), /):
        self.connection._gate.admit(statement=sql)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        self.connection._gate.admit(statement=sql)
        return super().executemany(sql, parameters)

    def executescript(self, sql_script, /):
        self.connection._gate.admit(verb="SCRIPT")
        return super().executescript(sql_script)


class GatedConnection(sqlite3.Connection):
    """sqlite3.Connection with one gate in front of every statement (Patch 14 §8.1, INV-D1).

    The creating thread is the owner. While a thread holds the worker scope (worker_scope), every other
    thread's writes are refused before they reach SQLite; a thread that neither owns the connection nor
    holds the scope is reported (REPORT) or refused (STRICT). backup, iterdump and blobopen would bypass
    the gate, and Cockpit doesn't use them, so they raise.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = _Gate(threading.get_ident())

    def configure_gate(self, mode: AccessMode, access_log: Optional[str] = None) -> None:
        self._gate.mode = mode
        self._gate.access_log = access_log

    def gate_counts(self) -> tuple[int, int]:
        """(writes refused, off-owner statements), since the connection opened."""
        return self._gate.refused, self._gate.off_owner

    def cursor(self, factory=None):
        if factory is None:
            factory = GatedCursor
        elif not (isinstance(factory, type) and issubclass(factory, GatedCursor)):
            raise TypeError("a GatedConnection's cursors must be GatedCursor, so none can bypass the gate")
        return super().cursor(factory)

    def execute(self, sql, parameters=(), /):
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql, parameters, /):
        return self.cursor().executemany(sql, parameters)

    def executescript(self, sql_script, /):
        return self.cursor().executescript(sql_script)

    def commit(self) -> None:
        self._gate.admit(verb="COMMIT")
        super().commit()

    def rollback(self) -> None:
        self._gate.admit(verb="ROLLBACK")
        super().rollback()

    def close(self) -> None:
        self._gate.admit(verb="CLOSE")
        super().close()

    def __exit__(self, exc_type, exc_value, traceback):
        self._gate.admit(verb="COMMIT")
        return super().__exit__(exc_type, exc_value, traceback)

    def backup(self, *args, **kwargs):
        raise NotImplementedError("Connection.backup isn't used by Cockpit and would bypass the write gate")

    def iterdump(self, *args, **kwargs):
        raise NotImplementedError("Connection.iterdump isn't used by Cockpit and would bypass the write gate")

    def blobopen(self, *args, **kwargs):
        raise NotImplementedError("Connection.blobopen isn't used by Cockpit and would bypass the write gate")


@contextlib.contextmanager
def worker_scope(conn: sqlite3.Connection, holder: str) -> Iterator[None]:
    """Marks the calling thread as the connection's worker for the block (Patch 14 §8.3).

    pre:  conn is a GatedConnection, or any other sqlite3.Connection, for which this does nothing
    post: while open, every statement from this thread passes and other threads' writes are refused; the
          same thread may nest it; leaving the block, however it ends, opens the gate again
    raises: ScopeConflict when another thread already holds the scope
    """
    gate = getattr(conn, "_gate", None)
    if gate is None:
        yield
        return
    gate.enter_scope(holder)
    try:
        yield
    finally:
        gate.exit_scope()


def access_mode_from_environment() -> tuple[AccessMode, Optional[str]]:
    """COCKPIT_DB_ACCESS=strict for tests (Patch 14 §8.5); REPORT otherwise. COCKPIT_DB_ACCESS_LOG names a file."""
    mode = AccessMode.STRICT if os.environ.get("COCKPIT_DB_ACCESS", "").strip().lower() == "strict" else AccessMode.REPORT
    return mode, (os.environ.get("COCKPIT_DB_ACCESS_LOG") or None)


def open_connection(
    db_path: pathlib.Path,
    cache_size_kib: int = -16384,
    mmap_size_bytes: int = 134217728,
    wal_autocheckpoint_pages: int = 1000,
    access_mode: Optional[AccessMode] = None,
) -> sqlite3.Connection:
    """
    Produce a SQLite connection with the invariants the access layer depends on.

    Patch 14 §8: it is a GatedConnection owned by the calling thread; access_mode defaults to the one
    access_mode_from_environment names.
    """
    if not db_path.parent.exists():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise PersistenceUnavailable(db_path, e)

    try:
        # isolation_level=None disables the sqlite3 module's implicit transaction
        # management, allowing us to explicitly use BEGIN IMMEDIATE / COMMIT.
        conn = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False, factory=GatedConnection,
        )
        environment_mode, access_log = access_mode_from_environment()
        conn.configure_gate(access_mode or environment_mode, access_log)
        
        # Enforce required PRAGMAs
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute(f"PRAGMA cache_size = {cache_size_kib};")
        conn.execute(f"PRAGMA mmap_size = {mmap_size_bytes};")
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute(f"PRAGMA wal_autocheckpoint = {wal_autocheckpoint_pages};")
        
        conn.row_factory = hydrating_row_factory
        return conn
    except sqlite3.Error as e:
        raise PersistenceUnavailable(db_path, e)


def run_idle_maintenance(conn: sqlite3.Connection) -> None:
    """
    Opportunistic maintenance; safe to call when no transaction is open.
    """
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    except sqlite3.Error as e:
        logger.warning("idle maintenance skipped", exc_info=e)
