"""Build Notes Checklist repository implementation."""

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
from ..types import BuildNoteItem, BuildNoteItemDraft, AuditStatus


class BuildNotesChecklistRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_many(self, items: Sequence[BuildNoteItemDraft]) -> list[BuildNoteItem]:
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

        cur.execute("SAVEPOINT insert_many_notes")
        results = []
        try:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO build_notes_checklist (
                        audit_id, source_file_id, row_sequence, original_text, is_verified, notes
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item.audit_id,
                        item.source_file_id,
                        item.row_sequence,
                        item.original_text,
                        item.is_verified,
                        item.notes
                    )
                )
                item_id = cur.lastrowid
                assert item_id is not None
                
                cur.execute("SELECT * FROM build_notes_checklist WHERE id = ?", (item_id,))
                row = cur.fetchone()
                assert row is not None
                results.append(BuildNoteItem(**row))
            cur.execute("RELEASE SAVEPOINT insert_many_notes")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT insert_many_notes")
            cur.execute("RELEASE SAVEPOINT insert_many_notes")
            raise

        return results

    def list_for_audit(self, audit_id: int) -> list[BuildNoteItem]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT * FROM build_notes_checklist WHERE audit_id = ? ORDER BY row_sequence ASC, id ASC",
            (audit_id,)
        )
        return [BuildNoteItem(**row) for row in cur.fetchall()]

    def set_verification(self, item_id: int, is_verified: bool, notes: str | None) -> BuildNoteItem:
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE build_notes_checklist SET is_verified = ?, notes = ? WHERE id = ?",
            (is_verified, notes, item_id)
        )
        if cur.rowcount == 0:
            raise ChecklistItemNotFound(item_id, "notes")
            
        cur.execute("SELECT * FROM build_notes_checklist WHERE id = ?", (item_id,))
        row = cur.fetchone()
        assert row is not None
        return BuildNoteItem(**row)

    def mark_all_verified(self, audit_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT status FROM active_audits WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            raise AuditNotFound(audit_id)
            
        if row["status"] == AuditStatus.COMPLETED.value:
            raise IllegalStateTransition(audit_id, AuditStatus.COMPLETED, AuditStatus.COMPLETED)
            
        cur.execute(
            "UPDATE build_notes_checklist SET is_verified = 1 WHERE audit_id = ? AND is_verified = 0",
            (audit_id,)
        )
        return cur.rowcount
