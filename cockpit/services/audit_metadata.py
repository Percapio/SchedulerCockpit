"""Service for managing audit metadata."""

import sqlite3
from datetime import date

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.errors import PersistenceError


class AuditMetadataService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._repo = AuditRepository(conn)

    def set_ship_date(self, audit_id: int, new_value: date | None) -> None:
        """Update the ship date for an audit."""
        try:
            self._repo.set_ship_date(audit_id, new_value)
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            raise PersistenceError(f"Failed to set ship date for audit {audit_id}", e) from e
