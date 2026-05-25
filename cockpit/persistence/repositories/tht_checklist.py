"""THT Checklist repository implementation."""

import sqlite3
from typing import Sequence

from ..errors import (
    AuditNotFound,
    ChecklistItemNotFound,
    ForeignKeyMismatch,
    IllegalStateTransition,
    InvalidArgumentError,
    SourceFileNotFound,
)
from ..types import ThtChecklistItem, ThtChecklistItemDraft, AuditStatus


class ThtChecklistRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_many(self, items: Sequence[ThtChecklistItemDraft]) -> list[ThtChecklistItem]:
        if not items:
            raise InvalidArgumentError("items", items, "Cannot be empty")
        
        audit_id = items[0].audit_id
        if any(item.audit_id != audit_id for item in items):
            raise InvalidArgumentError("items", items, "All items must share the same audit_id")

        cur = self.conn.cursor()
        
        cur.execute("SELECT id FROM active_audits WHERE id = ?", (audit_id,))
        if not cur.fetchone():
            raise AuditNotFound(audit_id)

        # Validate source_file_ids and their audit association
        for item in items:
            if item.source_file_id is not None:
                cur.execute("SELECT audit_id FROM source_files WHERE id = ?", (item.source_file_id,))
                row = cur.fetchone()
                if not row:
                    raise SourceFileNotFound(item.source_file_id)
                if row["audit_id"] != audit_id:
                    raise ForeignKeyMismatch(audit_id, item.source_file_id, row["audit_id"])

        cur.execute("SAVEPOINT insert_many_tht")
        results = []
        try:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO tht_verification_checklist (
                        audit_id, source_file_id, component_mpn, description, is_verified
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        item.audit_id,
                        item.source_file_id,
                        item.component_mpn,
                        item.description,
                        item.is_verified
                    )
                )
                item_id = cur.lastrowid
                assert item_id is not None
                
                cur.execute("SELECT * FROM tht_verification_checklist WHERE id = ?", (item_id,))
                row = cur.fetchone()
                assert row is not None
                results.append(ThtChecklistItem(**row))
            cur.execute("RELEASE SAVEPOINT insert_many_tht")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT insert_many_tht")
            cur.execute("RELEASE SAVEPOINT insert_many_tht")
            raise

        return results

    def list_for_audit(self, audit_id: int) -> list[ThtChecklistItem]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM tht_verification_checklist WHERE audit_id = ? ORDER BY id ASC", (audit_id,))
        return [ThtChecklistItem(**row) for row in cur.fetchall()]

    def set_verification(self, item_id: int, is_verified: bool) -> ThtChecklistItem:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE tht_verification_checklist SET is_verified = ? WHERE id = ?",
            (is_verified, item_id)
        )
        if cur.rowcount == 0:
            raise ChecklistItemNotFound(item_id, "tht")
            
        cur.execute("SELECT * FROM tht_verification_checklist WHERE id = ?", (item_id,))
        row = cur.fetchone()
        assert row is not None
        return ThtChecklistItem(**row)

    def mark_all_verified(self, audit_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            raise AuditNotFound(audit_id)
            
        if row["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(audit_id, AuditStatus.COMPLETED, AuditStatus.COMPLETED)
            
        cur.execute(
            "UPDATE tht_verification_checklist SET is_verified = 1 WHERE audit_id = ? AND is_verified = 0",
            (audit_id,)
        )
        return cur.rowcount
