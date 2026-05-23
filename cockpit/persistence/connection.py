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
            
        if name == "is_verified":
            d[name] = bool(val)
        elif name.endswith("_at"):
            # SQLite stores dates as ISO 8601 strings
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d[name] = dt
        elif name == "ship_date":
            from datetime import date
            d[name] = date.fromisoformat(val)
        elif name == "traveler_metadata":
            d[name] = json.loads(val)
        elif name == "local_storage_path":
            d[name] = pathlib.Path(val)
        else:
            d[name] = val
            
    return d


def open_connection(db_path: pathlib.Path) -> sqlite3.Connection:
    """
    Produce a SQLite connection with the invariants the access layer depends on.
    """
    if not db_path.parent.exists():
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise PersistenceUnavailable(db_path, e)
            
    try:
        # isolation_level=None disables the sqlite3 module's implicit transaction
        # management, allowing us to explicitly use BEGIN IMMEDIATE / COMMIT.
        conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
        
        # Enforce required PRAGMAs
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        
        conn.row_factory = hydrating_row_factory
        return conn
    except sqlite3.Error as e:
        raise PersistenceUnavailable(db_path, e)
