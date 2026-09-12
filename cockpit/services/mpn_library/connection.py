import sqlite3
import threading
from pathlib import Path
from typing import Optional

from cockpit.persistence.connection import hydrating_row_factory
from cockpit.services.library_errors import LibraryUnavailable

LIBRARY_BUSY_TIMEOUT_MS: int = 5000

class ThreadBoundConnection:
    """Wrapper that verifies it is only used on its owner thread."""
    def __init__(self, conn: sqlite3.Connection, owner_thread: int):
        self._conn = conn
        self._owner_thread = owner_thread

    def _check_thread(self):
        if threading.get_ident() != self._owner_thread:
            raise RuntimeError("LibraryConnection used on wrong thread")

    def cursor(self):
        self._check_thread()
        return self._conn.cursor()

    def execute(self, *args, **kwargs):
        self._check_thread()
        return self._conn.execute(*args, **kwargs)

    def executemany(self, *args, **kwargs):
        self._check_thread()
        return self._conn.executemany(*args, **kwargs)

    def commit(self):
        self._check_thread()
        self._conn.commit()

    def rollback(self):
        self._check_thread()
        self._conn.rollback()

    def close(self):
        self._check_thread()
        self._conn.close()

LibraryConnection = ThreadBoundConnection

def open_library_connection(db_path: Path, owner_thread: int) -> LibraryConnection:
    if not db_path.exists():
        raise LibraryUnavailable(db_path)
        
    try:
        # We handle thread affinity ourselves via the wrapper, 
        # but sqlite3 will enforce it too if check_same_thread=True,
        # but it defaults to current thread. Since we might open it on UI 
        # for a worker, we must set check_same_thread=False to let sqlite bypass,
        # and we enforce owner_thread ourselves in ThreadBoundConnection.
        # Wait, the architecture doc says:
        # "returns a connection carrying the same PRAGMA set as connection.open_connection
        # plus busy_timeout = LIBRARY_BUSY_TIMEOUT_MS, and the same hydrating row factory.
        # The connection is bound to owner_thread and is closed by owner_thread..."
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        
        # Enforce required PRAGMAs
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA temp_store = MEMORY;")
        
        # Set busy timeout
        conn.execute(f"PRAGMA busy_timeout = {LIBRARY_BUSY_TIMEOUT_MS};")
        
        conn.row_factory = hydrating_row_factory
        return ThreadBoundConnection(conn, owner_thread)
    except sqlite3.Error as e:
        raise LibraryUnavailable(db_path, type(e))
