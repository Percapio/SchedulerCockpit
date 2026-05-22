"""Source file repository implementation."""

import sqlite3

from ..clock import utcnow
from ..errors import AuditNotFound, InvalidArgumentError
from ..types import SourceFile, SourceFileDraft


class SourceFileRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def register(self, draft: SourceFileDraft) -> SourceFile:
        if not draft.file_hash or len(draft.file_hash) != 64 or not draft.file_hash.islower() or not all(c in "0123456789abcdef" for c in draft.file_hash):
            raise InvalidArgumentError("file_hash", draft.file_hash, "Must be a 64-character lowercase hex SHA-256 string")

        cur = self.conn.cursor()
        now_iso = utcnow().isoformat()

        try:
            cur.execute(
                """
                INSERT INTO source_files (
                    audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.audit_id,
                    draft.file_category,
                    draft.original_filename,
                    str(draft.local_storage_path),
                    draft.file_hash,
                    now_iso
                )
            )
        except sqlite3.IntegrityError as e:
            if "FOREIGN KEY constraint failed" in str(e):
                raise AuditNotFound(draft.audit_id) from e
            raise

        file_id = cur.lastrowid
        assert file_id is not None
        
        cur.execute("SELECT * FROM source_files WHERE id = ?", (file_id,))
        row = cur.fetchone()
        assert row is not None
        
        return SourceFile(**row)

    def list_for_audit(self, audit_id: int) -> list[SourceFile]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM source_files WHERE audit_id = ? ORDER BY id ASC", (audit_id,))
        return [SourceFile(**row) for row in cur.fetchall()]

    def reference_count(self, file_hash: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT audit_id) as cnt FROM source_files WHERE file_hash = ?", (file_hash,))
        row = cur.fetchone()
        if not row:
            return 0
        return row["cnt"]
