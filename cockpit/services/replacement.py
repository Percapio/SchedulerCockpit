"""What an operator gives up by replacing a stored audit.

The confirmation enumerates casualties rather than asking "Replace?". Nothing
derived from the incoming documents appears here: every field is something that
exists only because someone worked the stored audit, and that a fresh ingest
cannot reproduce.
"""

from dataclasses import dataclass

from cockpit.persistence.types import ActiveAudit, AuditStatus


@dataclass(frozen=True)
class ReplacementCasualty:
    split_suffix: str
    quantity: int
    status: AuditStatus
    article_revision: str
    ops_per_board_min: float | None
    is_labeled: bool
    are_photos_uploaded: bool
    tht_item_count: int

    @property
    def label(self) -> str:
        """How this row is named in the dialog."""
        return self.split_suffix or "(unsplit)"


def summarise_family(family: list[ActiveAudit], tht_repo) -> list[ReplacementCasualty]:
    """What replacing this family destroys, one entry per member.

    pre:  family is every row sharing an identity, as list_family returns it
    post: order matches family; each entry carries only operator-entered or
          operator-accumulated state

    The checklist figure is a size, not a progress: migrate_to_v15 dropped
    is_verified, so there is no per-item verified state left to report.
    """
    casualties = []
    for audit in family:
        try:
            tht_count = len(tht_repo.list_for_audit(audit.id))
        except Exception:
            tht_count = 0
        casualties.append(ReplacementCasualty(
            split_suffix=audit.split_suffix,
            quantity=audit.quantity,
            status=audit.status,
            article_revision=audit.article_revision or "FA",
            ops_per_board_min=audit.ops_per_board_min,
            is_labeled=audit.is_labeled,
            are_photos_uploaded=audit.are_photos_uploaded,
            tht_item_count=tht_count,
        ))
    return casualties
