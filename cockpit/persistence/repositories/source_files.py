"""Source file repository implementation."""

import sqlite3

from ..clock import utcnow
from ..errors import AuditNotFound, InvalidArgumentError, PersistenceInvariantViolation
from ..types import SourceFile, SourceFileDraft, SourceFileCategory


class SourceFileRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    @staticmethod
    def _check_hash(name: str, value: str | None) -> None:
        # The whitelist is the lowercase rule: str.islower() is False for an all-digit string.
        if not value or len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
            raise InvalidArgumentError(name, value, "Must be a 64-character lowercase hex SHA-256 string")

    def register(self, draft: SourceFileDraft) -> SourceFile:
        self._check_hash("file_hash", draft.file_hash)
        # Phase 57 §7.3: a copy's share hash is its file_hash; only an extract names another.
        source_hash = draft.source_hash if draft.source_hash is not None else draft.file_hash
        self._check_hash("source_hash", source_hash)

        cur = self.conn.cursor()
        now_iso = utcnow().isoformat()

        try:
            cur.execute(
                """
                INSERT INTO source_files (
                    audit_id, file_category, original_filename, local_storage_path, file_hash, ingested_at,
                    source_hash, source_size, source_modified_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.audit_id,
                    draft.file_category,
                    draft.original_filename,
                    str(draft.local_storage_path),
                    draft.file_hash,
                    now_iso,
                    source_hash,
                    draft.source_size,
                    draft.source_modified_ns,
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

    def replace(
        self,
        audit_id: int,
        category: SourceFileCategory,
        draft: SourceFileDraft,
    ) -> SourceFile:
        """Replaces an audit's row for one category with the draft.

        pre:  called inside the caller's savepoint
        post: the audit holds exactly one row for category, the draft's; the
              old row's side-table rows went with it (ON DELETE CASCADE); the
              old file is not touched, because reaping it is the caller's, after
              RELEASE and behind reference_count
        raises: InvalidArgumentError when the draft names another audit or
                category, or carries a malformed hash; AuditNotFound

        The drawing path still does this with raw SQL inside the ingestion
        service (add_pdf_to_audit); this is the seam it can adopt.
        """
        if draft.audit_id != audit_id or draft.file_category != category:
            raise InvalidArgumentError(
                "draft", draft, "Must name the audit and category being replaced"
            )
        self.conn.execute(
            "DELETE FROM source_files WHERE audit_id = ? AND file_category = ?",
            (audit_id, category.value),
        )
        return self.register(draft)

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

    def find_by_audit_and_category(
        self,
        audit_id: int,
        category: SourceFileCategory,
    ) -> SourceFile | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM source_files WHERE audit_id = ? AND file_category = ?",
            (audit_id, category.value)
        )
        rows = cur.fetchall()
        
        if not rows:
            # AuditNotFound isn't strictly required here unless we want to query `active_audits`
            # The spec says: "Returns None if no row exists for this pair".
            # "Raises AuditNotFound (re-raises Phase 1's existing pattern)".
            # Actually, to raise AuditNotFound we'd need to verify the audit exists,
            # but usually we just return None. Let's do a quick check if audit exists.
            cur.execute("SELECT id FROM active_audits WHERE id = ?", (audit_id,))
            if not cur.fetchone():
                raise AuditNotFound(audit_id)
            return None
            
        if len(rows) > 1:
            raise PersistenceInvariantViolation(
                f"Multiple source files found for audit_id={audit_id} category={category.value}"
            )
            
        return SourceFile(**rows[0])
