"""Audit read service."""

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.services.views import OpenAuditDigest


class AuditReadService:
    def __init__(self, audit_repo: AuditRepository) -> None:
        self._audit_repo = audit_repo

    def list_open(self) -> list[OpenAuditDigest]:
        audits = self._audit_repo.list_open()
        return [
            OpenAuditDigest(
                audit_id=a.id,
                part_number=a.part_number,
                work_order_ref=a.work_order_ref,
                split_suffix=a.split_suffix,
                quantity=a.quantity,
                status=a.status,
                updated_at=a.updated_at
            )
            for a in audits
        ]
