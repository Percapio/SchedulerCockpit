"""Split service."""

import sqlite3

from cockpit.persistence.errors import InvalidArgumentError, IllegalStateTransition
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.types import AuditStatus
from cockpit.services.views import SplitSummary


class AuditSplitService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        audit_repo: AuditRepository,
    ) -> None:
        self._conn = conn
        self._audit_repo = audit_repo

    def split(
        self,
        source_audit_id: int,
        source_new_suffix: str | None,
        sibling_suffix: str,
        sibling_quantity: int,
        reason: str,
    ) -> SplitSummary:
        source = self._audit_repo.find_by_id(source_audit_id)
        # Note: Phase 1 repositories raise AuditNotFound on find_by_id if not found?
        # Wait, find_by_id returns None if not found!
        if source is None:
            # Let's import AuditNotFound
            from cockpit.persistence.errors import AuditNotFound
            raise AuditNotFound(source_audit_id)

        if source.status not in (AuditStatus.PENDING, AuditStatus.IN_PROGRESS):
            raise IllegalStateTransition(source_audit_id, source.status, AuditStatus.PENDING)

        if source.split_suffix == "":
            if source_new_suffix is None:
                raise InvalidArgumentError("source_new_suffix", source_new_suffix, "Required when source is un-split")
        else:
            if source_new_suffix is not None:
                raise InvalidArgumentError("source_new_suffix", source_new_suffix, "Must be None when source is already split")

        if source_new_suffix is not None and source_new_suffix == sibling_suffix:
            raise InvalidArgumentError("sibling_suffix", sibling_suffix, "Must not equal source_new_suffix")
            
        if sibling_quantity >= source.quantity:
            raise InvalidArgumentError("sibling_quantity", sibling_quantity, "Must be less than source.quantity")

        cur = self._conn.cursor()
        cur.execute("SAVEPOINT split")
        try:
            if source.split_suffix == "" and source_new_suffix is not None:
                self._audit_repo.relabel_suffix(source.id, source_new_suffix)

            self._audit_repo.clone_to_suffix(source.id, sibling_suffix, sibling_quantity, reason)
            self._audit_repo.set_split_reason(source.id, reason)
            self._audit_repo.set_quantity(source.id, source.quantity - sibling_quantity)
            
            cur.execute("RELEASE SAVEPOINT split")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT split")
            cur.execute("RELEASE SAVEPOINT split")
            raise

        return SplitSummary(sibling_suffix, sibling_quantity)
