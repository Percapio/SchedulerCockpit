"""Document updates: BOM, ECO and traveler revisions from the share.

Phase 55. The work is split across threads so the worker never touches the
shared connection:

- snapshot_family (UI thread) reads, as values, everything the later stages
  need from the database;
- probe_family and build_preview (update worker) read the share and write
  nothing anywhere;
- prepare_family_update (update worker) copies into managed storage and writes
  no rows;
- apply_family_update and discard_copies (UI thread) write, one savepoint per
  family.
"""

import collections
import logging
import os
import pathlib
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Final, Mapping, Sequence, Union

from cockpit.ingestion import hashing
from cockpit.ingestion.cross_validation import _sanitize_for_json
from cockpit.ingestion.errors import HashingError, JobDirectoryEscapesRoot
from cockpit.ingestion.filename_rules import article_designation
from cockpit.ingestion.gatekeeper import ACCEPTED_EXTENSIONS
from cockpit.ingestion.locator import (
    assert_within_root,
    choose_current_version,
    resolve_job_directory,
)
from cockpit.ingestion.parsers import audit_bom
from cockpit.ingestion.parsers.results import BomItem, BomResult, EcoResult
from cockpit.ingestion.roles import SourceRole, role_of
from cockpit.ingestion.service import content_addressed_path
from cockpit.ingestion.version_tags import Dated, VersionTag, tag_rank, version_tag
from cockpit.layout.results import PdfComponentCoordinate
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.placements import (
    BoardPlacementRepository,
    PageRegistrationRepository,
    PlacementRejectionRepository,
)
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.types import (
    ActiveAudit,
    AuditBomComponent,
    AuditBomComponentDraft,
    PdfComponentCoordDraft,
    PlacementRejection,
    SourceFile,
    SourceFileCategory,
    SourceFileDraft,
    ThtChecklistItemDraft,
)
from cockpit.protocols import ParserRegistry
from cockpit.services.storage_reaper import StorageReaper

logger = logging.getLogger(__name__)

UPDATED_ROLES: Final[tuple[SourceRole, ...]] = (
    SourceRole.BOM, SourceRole.NOTES, SourceRole.TRAVELER,
)

CATEGORY_OF_ROLE: Final[Mapping[SourceRole, SourceFileCategory]] = {
    SourceRole.BOM: SourceFileCategory.BOM,
    SourceRole.NOTES: SourceFileCategory.NOTES,
    SourceRole.TRAVELER: SourceFileCategory.TRAVELER,
    SourceRole.PDF: SourceFileCategory.PDF,
    SourceRole.PLACEMENTS: SourceFileCategory.PLACEMENTS,
}

# Phase 57 §8: placements have their own probe, prepare and apply, so they aren't in UPDATED_ROLES.
PROBED_ROLES: Final[frozenset[SourceRole]] = frozenset(UPDATED_ROLES) | {SourceRole.PLACEMENTS}

ROLE_OF_CATEGORY: Final[Mapping[SourceFileCategory, SourceRole]] = {
    category: role for role, category in CATEGORY_OF_ROLE.items()
}

FIRST_ARTICLE: Final[str] = "FA"

UNSPLIT_DIRECTORY: Final[str] = "unsplit"

PARTIAL_COPY_SUFFIX: Final[str] = ".partial"


# ---------- what the share holds ----------

@dataclass(frozen=True)
class CandidateFile:
    path: pathlib.Path
    name: str
    size: int
    modified_at_ns: int
    tag: VersionTag


@dataclass(frozen=True)
class StoredDocument:
    """A role's managed copy, as one member holds it.

    size and modified_at_ns are None when the stored copy can't be stat'ed,
    which only disables the fast path: the candidate is then hashed.

    Phase 57 §8.1: source_hash is the share file's hash, equal to file_hash for
    a copy; source_size and source_modified_ns are the share file's, filled for
    a Placements extract only.
    """

    role: SourceRole
    source_file_id: int
    original_filename: str
    stored_path: pathlib.Path
    file_hash: str
    size: int | None
    modified_at_ns: int | None
    source_hash: str = ""
    source_size: int | None = None
    source_modified_ns: int | None = None

    def __post_init__(self) -> None:
        if not self.source_hash:
            object.__setattr__(self, "source_hash", self.file_hash)


@dataclass(frozen=True)
class Unchanged:
    pass


@dataclass(frozen=True)
class Changed:
    candidate: CandidateFile
    candidate_hash: str
    members_to_update: tuple[int, ...]


@dataclass(frozen=True)
class NeedsChoice:
    candidates: tuple[CandidateFile, ...]


@dataclass(frozen=True)
class OlderOnShare:
    candidate: CandidateFile
    candidate_hash: str
    members_to_update: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class Missing:
    searched: pathlib.Path


@dataclass(frozen=True)
class NotStored:
    pass


FamilyDocumentStatus = Union[Unchanged, Changed, NeedsChoice, OlderOnShare, Missing, NotStored]


@dataclass(frozen=True)
class ShareReadFailure:
    path: pathlib.Path
    cause: str


@dataclass(frozen=True)
class ShareUnreachable:
    cause: str


@dataclass(frozen=True)
class EscapesRoot:
    path: pathlib.Path


@dataclass(frozen=True)
class ArticleFolderAmbiguous:
    candidates: tuple[pathlib.Path, ...]


@dataclass(frozen=True)
class CandidateUnreadable:
    path: pathlib.Path
    cause: str


ProbeFailure = Union[ShareUnreachable, EscapesRoot, ArticleFolderAmbiguous, CandidateUnreadable]

HashReader = Callable[[CandidateFile], Union[str, ShareReadFailure]]


# ---------- what the database holds, read on the UI thread ----------

@dataclass(frozen=True, order=True)
class FamilyKey:
    part_number: str
    work_order_ref: str


@dataclass(frozen=True)
class FamilyMember:
    audit_id: int
    split_suffix: str
    quantity: int
    designators: frozenset[str]


@dataclass(frozen=True)
class HeldState:
    """The reference member's rows the preview diffs against."""

    components: tuple[AuditBomComponent, ...]
    tht_items: tuple[tuple[str, str | None], ...]
    traveler_metadata: Mapping[str, Any] | None
    ops_per_board_min: float | None


@dataclass(frozen=True)
class FamilySnapshot:
    key: FamilyKey
    members: tuple[FamilyMember, ...]
    reference_member: int
    article_revision: str
    stored: Mapping[int, Mapping[SourceRole, StoredDocument]]
    held: HeldState
    # Phase 57 §8.1: each member's recorded .bmb rejections and refusals, latest first.
    rejections: Mapping[int, tuple[PlacementRejection, ...]] = field(default_factory=dict)

    @property
    def family(self) -> tuple[int, ...]:
        return tuple(member.audit_id for member in self.members)

    @property
    def family_quantity(self) -> int:
        return sum(member.quantity for member in self.members)


@dataclass(frozen=True)
class FamilyProbe:
    """One family's probe.

    Phase 57 §8.2: placements is None when the run doesn't include PLACEMENTS;
    otherwise decide_placements' status, or the ShareReadFailure of a .bmb that
    couldn't be read, which never fails the family's other roles.
    placement_candidates are the job root's .bmb files.
    """

    snapshot: FamilySnapshot
    folder: pathlib.Path | None
    statuses: Mapping[SourceRole, FamilyDocumentStatus]
    placements: Any = None
    placement_candidates: tuple[CandidateFile, ...] = ()

    @property
    def key(self) -> FamilyKey:
        return self.snapshot.key


@dataclass(frozen=True)
class FamilyUpdateRepositories:
    conn: sqlite3.Connection
    audits: AuditRepository
    source_files: SourceFileRepository
    bom_components: AuditBomComponentRepository
    tht_checklist: ThtChecklistRepository
    pdf_coords: PdfComponentCoordRepository
    board_placements: BoardPlacementRepository
    page_registrations: PageRegistrationRepository
    placement_rejections: PlacementRejectionRepository


# ---------- failures of prepare and apply ----------

@dataclass(frozen=True)
class StorageFailed:
    cause: str


@dataclass(frozen=True)
class DocumentRejected:
    role: SourceRole
    reason: str


@dataclass(frozen=True)
class IdentityMismatch:
    role: SourceRole
    field_key: str
    expected: str
    found: str


@dataclass(frozen=True)
class CandidateChangedSincePreview:
    role: SourceRole


@dataclass(frozen=True)
class FamilyChangedSincePreview:
    family: tuple[int, ...]


@dataclass(frozen=True)
class WriteFailed:
    cause: str


@dataclass(frozen=True)
class Cancelled:
    pass


FamilyUpdateFailure = Union[
    StorageFailed, DocumentRejected, IdentityMismatch, CandidateChangedSincePreview,
    FamilyChangedSincePreview, WriteFailed, Cancelled,
]


def _describe(error: BaseException) -> str:
    """A cause fit for logs: the exception's kind and code, never its message.

    Messages carry share paths, and share paths carry job folder names.
    """
    if isinstance(error, HashingError):
        return _describe(error.cause)
    code = getattr(error, "winerror", None) or getattr(error, "errno", None)
    if code is not None:
        return f"{type(error).__name__} ({code})"
    reason = getattr(error, "reason", None)
    if isinstance(reason, str) and reason:
        return f"{type(error).__name__} ({reason})"
    return type(error).__name__


# ---------- section 3: finding the current version ----------

def read_share_hash(candidate: CandidateFile) -> str | ShareReadFailure:
    """Hashes a candidate on the share; a read that fails is a value, not a raise."""
    try:
        return hashing.sha256_hex(candidate.path)
    except (HashingError, OSError) as error:
        return ShareReadFailure(candidate.path, _describe(error))


def _older_reason(candidate: CandidateFile, reference: StoredDocument) -> str | None:
    stored_tag = version_tag(reference.original_filename)
    if isinstance(stored_tag, Dated) or isinstance(candidate.tag, Dated):
        if tag_rank(stored_tag) <= tag_rank(candidate.tag):
            return None
        if isinstance(candidate.tag, Dated):
            return (
                f"Cockpit holds a copy dated {stored_tag.date:%m-%d-%Y}; "
                f"the share's is dated {candidate.tag.date:%m-%d-%Y}"
            )
        return f"Cockpit holds a copy dated {stored_tag.date:%m-%d-%Y}; the share's is undated"
    if reference.modified_at_ns is not None and candidate.modified_at_ns < reference.modified_at_ns:
        return "The share's file was last modified before the copy Cockpit holds"
    return None


def decide_role(
    role: SourceRole,
    candidates: Sequence[CandidateFile],
    reference: StoredDocument,
    member_hashes: Mapping[int, str],
    searched: pathlib.Path,
    read_hash: HashReader,
) -> FamilyDocumentStatus | ShareReadFailure:
    """Decides one role's status for a family.

    pre:  candidates are this role's files in the family's folder; reference is
          the reference member's stored document for the role; member_hashes
          holds every member's stored hash for it
    post: the first of these that applies, in this order:
            1. no candidates: Missing
            2. choose_current_version over the candidates' names; none: NeedsChoice
            3. every member's hash is equal, and the chosen candidate's name,
               size and modification time equal the reference copy's:
               Unchanged, without reading the candidate
            4. read the candidate's hash; a ShareReadFailure is returned as such
            5. every member's hash equals it: Unchanged
            6. the reference name's tag ranks above the candidate's, or neither
               name is Dated and the candidate's modification time is earlier
               than the reference copy's: OlderOnShare
            7. otherwise Changed, with members_to_update the members whose
               hash differs
    """
    if not candidates:
        return Missing(searched)

    chosen_name = choose_current_version(role, [c.name for c in candidates])
    if chosen_name is None:
        return NeedsChoice(tuple(sorted(candidates, key=lambda c: c.name)))
    chosen = next(c for c in candidates if c.name == chosen_name)

    family_is_consistent = len(set(member_hashes.values())) == 1
    if (
        family_is_consistent
        and chosen.name == reference.original_filename
        and chosen.size == reference.size
        and chosen.modified_at_ns == reference.modified_at_ns
    ):
        return Unchanged()

    candidate_hash = read_hash(chosen)
    if isinstance(candidate_hash, ShareReadFailure):
        return candidate_hash

    lagging = tuple(sorted(a for a, h in member_hashes.items() if h != candidate_hash))
    if not lagging:
        return Unchanged()

    older = _older_reason(chosen, reference)
    if older is not None:
        return OlderOnShare(chosen, candidate_hash, lagging, older)
    return Changed(chosen, candidate_hash, lagging)


def _job_root(
    snapshot: FamilySnapshot,
    source_root: pathlib.Path,
) -> pathlib.Path | Missing | ProbeFailure:
    """The family's job folder on the share, as ingest would find it, checked inside source_root."""
    try:
        job_dir = resolve_job_directory(snapshot.key.part_number, source_root)
        if not job_dir.is_dir():
            return Missing(job_dir)
    except OSError as error:
        return ShareUnreachable(_describe(error))
    try:
        assert_within_root(job_dir, source_root)
    except JobDirectoryEscapesRoot:
        return EscapesRoot(job_dir)
    return job_dir


def _is_first_article(snapshot: FamilySnapshot) -> bool:
    return (snapshot.article_revision or FIRST_ARTICLE).strip().lower() == FIRST_ARTICLE.lower()


def _family_folder(
    snapshot: FamilySnapshot,
    source_root: pathlib.Path,
) -> pathlib.Path | Missing | ProbeFailure:
    """The folder the family was first ingested from, as ingest would find it."""
    job_dir = _job_root(snapshot, source_root)
    if not isinstance(job_dir, pathlib.Path):
        return job_dir

    article = (snapshot.article_revision or FIRST_ARTICLE).strip().lower()
    if _is_first_article(snapshot):
        return job_dir

    try:
        matches = sorted(
            (
                child for child in job_dir.iterdir()
                if child.is_dir() and (article_designation(child.name) or "") == article
            ),
            key=lambda p: p.name,
        )
    except OSError as error:
        return ShareUnreachable(_describe(error))
    if not matches:
        return Missing(job_dir)
    if len(matches) > 1:
        return ArticleFolderAmbiguous(tuple(matches))
    try:
        assert_within_root(matches[0], source_root)
    except JobDirectoryEscapesRoot:
        return EscapesRoot(matches[0])
    return matches[0]


def _placement_candidates(
    job_root: pathlib.Path,
    entries: Sequence[pathlib.Path] | None,
) -> tuple[CandidateFile, ...] | ShareReadFailure:
    """The job root's .bmb files (Phase 57 §8.2), from the family folder's listing when it is the job root."""
    try:
        listing = list(entries) if entries is not None else list(job_root.iterdir())
    except OSError as error:
        return ShareReadFailure(job_root, _describe(error))
    found: list[CandidateFile] = []
    for entry in listing:
        if role_of(entry.name) != SourceRole.PLACEMENTS:
            continue
        try:
            if not entry.is_file():
                continue
            stat = entry.stat()
        except OSError as error:
            return ShareReadFailure(entry, _describe(error))
        found.append(CandidateFile(
            path=entry, name=entry.name, size=stat.st_size,
            modified_at_ns=stat.st_mtime_ns, tag=version_tag(entry.name),
        ))
    return tuple(sorted(found, key=lambda c: c.name))


def probe_family(
    snapshot: FamilySnapshot,
    source_root: pathlib.Path,
    read_hash: HashReader = read_share_hash,
    roles: frozenset[SourceRole] = PROBED_ROLES,
) -> FamilyProbe | ProbeFailure:
    """Reads the share for one family without writing anything anywhere.

    pre:  runs on the update worker; snapshot was read on the UI thread
    post: one directory listing of the family's folder (plus the job root for
          an article's folder lookup); one status per role in UPDATED_ROLES
          that roles names, NotStored for a role any member lacks; a candidate
          is only ever read after it resolved inside source_root; a candidate
          that can't be stat'ed or read is CandidateUnreadable for this family
          only. Phase 57 §8.2: when roles names PLACEMENTS, the job root's
          .bmb files are listed (one more listing for an article's folder) and
          decided by decide_placements; a .bmb that can't be listed, stat'ed or
          read is the placements status, never the family's failure
    """
    folder = _family_folder(snapshot, source_root)
    if isinstance(folder, (ShareUnreachable, EscapesRoot, ArticleFolderAmbiguous)):
        return folder

    probed = tuple(role for role in UPDATED_ROLES if role in roles)
    candidates_by_role: dict[SourceRole, list[CandidateFile]] = {role: [] for role in probed}
    entries: list[pathlib.Path] | None = None
    if isinstance(folder, pathlib.Path):
        try:
            entries = list(folder.iterdir())
        except OSError as error:
            return ShareUnreachable(_describe(error))
        for entry in entries:
            role = role_of(entry.name)
            if role not in candidates_by_role:
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError as error:
                return CandidateUnreadable(entry, _describe(error))
            candidates_by_role[role].append(CandidateFile(
                path=entry, name=entry.name, size=stat.st_size,
                modified_at_ns=stat.st_mtime_ns, tag=version_tag(entry.name),
            ))

    escaped: list[pathlib.Path] = []

    def contained_read(candidate: CandidateFile) -> str | ShareReadFailure:
        try:
            assert_within_root(candidate.path, source_root)
        except JobDirectoryEscapesRoot:
            escaped.append(candidate.path)
            return ShareReadFailure(candidate.path, "ESCAPES_ROOT")
        return read_hash(candidate)

    statuses: dict[SourceRole, FamilyDocumentStatus] = {}
    for role in probed:
        held = [snapshot.stored.get(member.audit_id, {}).get(role) for member in snapshot.members]
        if any(document is None for document in held):
            statuses[role] = NotStored()
            continue
        if isinstance(folder, Missing):
            statuses[role] = folder
            continue
        status = decide_role(
            role,
            candidates_by_role[role],
            snapshot.stored[snapshot.reference_member][role],
            {member.audit_id: snapshot.stored[member.audit_id][role].source_hash for member in snapshot.members},
            folder,
            contained_read,
        )
        if isinstance(status, ShareReadFailure):
            if escaped:
                return EscapesRoot(escaped[0])
            return CandidateUnreadable(status.path, status.cause)
        statuses[role] = status

    placements: Any = None
    placement_candidates: tuple[CandidateFile, ...] = ()
    if SourceRole.PLACEMENTS in roles:
        placements, placement_candidates = _probe_placements(snapshot, source_root, folder, entries, read_hash)

    return FamilyProbe(
        snapshot=snapshot,
        folder=folder if isinstance(folder, pathlib.Path) else None,
        statuses=statuses,
        placements=placements,
        placement_candidates=placement_candidates,
    )


def _probe_placements(
    snapshot: FamilySnapshot,
    source_root: pathlib.Path,
    folder: pathlib.Path | Missing,
    entries: list[pathlib.Path] | None,
    read_hash: HashReader,
) -> tuple[Any, tuple[CandidateFile, ...]]:
    """Phase 57 §8.2: the job root's .bmb, decided member by member; a failure stays placements'."""
    from cockpit.services import placement_updates

    if _is_first_article(snapshot):
        job_root: pathlib.Path | Missing | ProbeFailure = folder
        listing = entries
    else:
        job_root = _job_root(snapshot, source_root)
        listing = None
    if isinstance(job_root, Missing):
        return placement_updates.NoBmb(), ()
    if isinstance(job_root, EscapesRoot):
        return ShareReadFailure(job_root.path, "ESCAPES_ROOT"), ()
    if not isinstance(job_root, pathlib.Path):
        return ShareReadFailure(source_root, getattr(job_root, "cause", type(job_root).__name__)), ()

    candidates = _placement_candidates(job_root, listing)
    if isinstance(candidates, ShareReadFailure):
        return candidates, ()

    def contained_read(candidate: CandidateFile) -> str | ShareReadFailure:
        try:
            assert_within_root(candidate.path, source_root)
        except JobDirectoryEscapesRoot:
            return ShareReadFailure(candidate.path, "ESCAPES_ROOT")
        return read_hash(candidate)

    return placement_updates.decide_placements(candidates, snapshot, contained_read), candidates


# ---------- section 4: the preview ----------

@dataclass(frozen=True, order=True)
class BomLineKey:
    find_number: str
    component_mpn: str


@dataclass(frozen=True)
class BomDiff:
    lines_added: tuple[BomLineKey, ...]
    lines_removed: tuple[BomLineKey, ...]
    lines_changed: tuple[BomLineKey, ...]
    designators_added: frozenset[str]
    designators_removed: frozenset[str]
    tht_items_added: int
    tht_items_removed: int
    cleaning: audit_bom.CleaningSummary


@dataclass(frozen=True)
class FieldChange:
    field_key: str
    before: str
    after: str


@dataclass(frozen=True)
class TravelerDiff:
    changed_fields: tuple[FieldChange, ...]
    quantity_now: int
    quantity_on_traveler: int


@dataclass(frozen=True)
class RequirementDiff:
    """Filled only once Phase 54 is present; before and after are its summaries."""

    before: Any | None
    after: Any
    tick_will_need_recheck: bool


RequirementPreviewer = Callable[[FamilyProbe, Mapping[SourceRole, pathlib.Path]], Union[RequirementDiff, None]]


@dataclass(frozen=True)
class RoleUpdate:
    role: SourceRole
    candidate: CandidateFile
    candidate_hash: str
    members_to_update: tuple[int, ...]
    older_reason: str | None


@dataclass(frozen=True)
class FamilyPreview:
    """One family's dry run. Holds diffs only: every parsed document is dropped once diffed."""

    probe: FamilyProbe
    updates: Mapping[SourceRole, RoleUpdate]
    bom: BomDiff | None
    traveler: TravelerDiff | None
    requirements: RequirementDiff | None
    ops_per_board_review: bool
    rejected: Mapping[SourceRole, Union[DocumentRejected, IdentityMismatch]]
    awaiting_choice: tuple[SourceRole, ...]
    # Phase 57 §8.3: why the job's .bmb can't be offered (a read or a pick that failed). Never blocks.
    placements_unavailable: str | None = None

    @property
    def can_apply(self) -> bool:
        return bool(self.updates) and not self.rejected and not self.awaiting_choice

    @property
    def ticked_by_default(self) -> bool:
        return self.can_apply and all(u.older_reason is None for u in self.updates.values())


@dataclass(frozen=True)
class _TravelerReading:
    metadata: dict[str, Any]
    quantity: int


def _gate_reason(path: pathlib.Path) -> str | None:
    """The per-file checks gatekeeper.validate makes of a dropped file."""
    if path.name.startswith("~$"):
        return "LOCK_FILE"
    if path.suffix.lower() not in ACCEPTED_EXTENSIONS:
        return "UNSUPPORTED_EXTENSION"
    return None


def _read_bom(path: pathlib.Path, parsers: ParserRegistry, key: FamilyKey) -> BomResult | DocumentRejected | IdentityMismatch:
    gate = _gate_reason(path)
    if gate is not None:
        return DocumentRejected(SourceRole.BOM, gate)
    try:
        result = parsers.bom_parser.parse(path)
    except Exception as error:
        return DocumentRejected(SourceRole.BOM, _describe(error))
    declared = result.declared_part_number.strip()
    if declared != key.part_number:
        return IdentityMismatch(SourceRole.BOM, "part_number", key.part_number, declared)
    return result


def _read_eco(path: pathlib.Path, parsers: ParserRegistry, key: FamilyKey) -> EcoResult | DocumentRejected | IdentityMismatch:
    gate = _gate_reason(path)
    if gate is not None:
        return DocumentRejected(SourceRole.NOTES, gate)
    try:
        result = parsers.eco_parser.parse(path)
    except Exception as error:
        return DocumentRejected(SourceRole.NOTES, _describe(error))
    declared = result.declared_part_number.strip()
    if declared != key.part_number:
        return IdentityMismatch(SourceRole.NOTES, "part_number", key.part_number, declared)
    return result


def _read_traveler(path: pathlib.Path, parsers: ParserRegistry, key: FamilyKey) -> _TravelerReading | DocumentRejected | IdentityMismatch:
    """Parses a traveler and applies reconcile's traveler checks.

    The identity fields are the traveler map's, exactly as reconcile reads
    them: part_number_field against the family's part number and
    work_order_ref_field (the S/O) against its work-order reference.
    """
    gate = _gate_reason(path)
    if gate is not None:
        return DocumentRejected(SourceRole.TRAVELER, gate)
    try:
        result = parsers.traveler_parser.parse(path, parsers.coord_map)
    except Exception as error:
        return DocumentRejected(SourceRole.TRAVELER, _describe(error))

    mapping = parsers.coord_map.identity_mapping
    fields = result.extracted_fields
    for field_key, expected in (
        (mapping.part_number_field, key.part_number),
        (mapping.work_order_ref_field, key.work_order_ref),
    ):
        found = str(fields.get(field_key) or "").strip()
        if found != expected:
            return IdentityMismatch(SourceRole.TRAVELER, field_key, expected, found)

    quantity = fields.get(mapping.quantity_field)
    if not isinstance(quantity, int) or quantity < 1:
        return DocumentRejected(SourceRole.TRAVELER, "QUANTITY_INVALID")
    # Ingest stores reconcile's sanitised copy; anything else would make an
    # updated audit's metadata differ from a freshly ingested one's.
    return _TravelerReading(metadata=_sanitize_for_json(fields), quantity=quantity)


_LineContent = tuple[str, Union[str, None], frozenset[str]]


def _held_lines(components: Sequence[AuditBomComponent]) -> dict[BomLineKey, _LineContent]:
    lines: dict[BomLineKey, _LineContent] = {}
    for component in components:
        key = BomLineKey(str(component.find_number), component.component_mpn)
        mount, description, designators = lines.get(
            key, (component.mount_type, component.description, frozenset())
        )
        lines[key] = (mount, description, designators | {component.ref_des})
    return lines


def _parsed_lines(items: Sequence[BomItem]) -> dict[BomLineKey, _LineContent]:
    lines: dict[BomLineKey, _LineContent] = {}
    for item in items:
        if not item.ref_des_list:
            continue
        key = BomLineKey(str(item.find_number), item.component_mpn)
        mount, description, designators = lines.get(
            key, (item.mount_type, item.description, frozenset())
        )
        lines[key] = (mount, description, designators | frozenset(item.ref_des_list))
    return lines


def diff_bom(held: HeldState, revised: BomResult) -> BomDiff:
    """What applying revised would change in the reference member's BOM rows.

    post: lines are compared like for like: only lines with designators, since
          only those have component rows; THT items are compared as a multiset
          of (part number, description), since that is all a THT row holds
    """
    before = _held_lines(held.components)
    after = _parsed_lines(revised.items)
    designators_before = frozenset().union(*(content[2] for content in before.values()))
    designators_after = frozenset().union(*(content[2] for content in after.values()))

    tht_before = collections.Counter(held.tht_items)
    tht_after = collections.Counter(
        (item.component_mpn, item.description) for item in revised.items if item.mount_type == "T"
    )

    cleaning = (
        audit_bom.summarise_cleaning(revised.cleaning)
        if revised.cleaning is not None
        else audit_bom.EMPTY_CLEANING_SUMMARY
    )
    return BomDiff(
        lines_added=tuple(sorted(after.keys() - before.keys())),
        lines_removed=tuple(sorted(before.keys() - after.keys())),
        lines_changed=tuple(sorted(k for k in after.keys() & before.keys() if after[k] != before[k])),
        designators_added=designators_after - designators_before,
        designators_removed=designators_before - designators_after,
        tht_items_added=sum((tht_after - tht_before).values()),
        tht_items_removed=sum((tht_before - tht_after).values()),
        cleaning=cleaning,
    )


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def diff_traveler(
    held: HeldState,
    revised: _TravelerReading,
    quantity_field: str,
    family_quantity: int,
) -> TravelerDiff:
    """Every field that would change, and the quantity that won't."""
    before = dict(held.traveler_metadata or {})
    keys = sorted((before.keys() | revised.metadata.keys()) - {quantity_field})
    return TravelerDiff(
        changed_fields=tuple(
            FieldChange(key, _as_text(before.get(key)), _as_text(revised.metadata.get(key)))
            for key in keys
            if _as_text(before.get(key)) != _as_text(revised.metadata.get(key))
        ),
        quantity_now=family_quantity,
        quantity_on_traveler=revised.quantity,
    )


def _pick_update(
    role: SourceRole,
    status: NeedsChoice,
    pick: pathlib.Path,
    probe: FamilyProbe,
    source_root: pathlib.Path,
    read_hash: HashReader,
) -> RoleUpdate | DocumentRejected | None:
    """Turns your pick for a NeedsChoice role into an update, re-checking the path."""
    candidate = next((c for c in status.candidates if c.path == pick), None)
    if candidate is None:
        return DocumentRejected(role, "PICK_NOT_A_CANDIDATE")
    try:
        assert_within_root(candidate.path, source_root)
    except JobDirectoryEscapesRoot:
        return DocumentRejected(role, "ESCAPES_ROOT")
    candidate_hash = read_hash(candidate)
    if isinstance(candidate_hash, ShareReadFailure):
        return DocumentRejected(role, candidate_hash.cause)
    snapshot = probe.snapshot
    lagging = tuple(sorted(
        member.audit_id for member in snapshot.members
        if snapshot.stored[member.audit_id][role].source_hash != candidate_hash
    ))
    if not lagging:
        return None
    return RoleUpdate(role, candidate, candidate_hash, lagging, older_reason=None)


def build_preview(
    probe: FamilyProbe,
    choices: Mapping[SourceRole, pathlib.Path],
    parsers: ParserRegistry,
    source_root: pathlib.Path,
    requirements: RequirementPreviewer | None = None,
    read_hash: HashReader = read_share_hash,
) -> FamilyPreview:
    """Dry-runs one family's candidates against what it holds.

    pre:  runs on the update worker
    post: each role that is Changed, OlderOnShare, or NeedsChoice with a pick in
          choices is parsed from the share as ingest parses it; a candidate that
          fails a gate or its parser, or whose declared identity differs from
          the family's key, is in rejected with the reason; a NeedsChoice role
          without a pick is in awaiting_choice and has no diff; requirements is
          filled only when a previewer is given; nothing is copied or written
    """
    snapshot = probe.snapshot
    updates: dict[SourceRole, RoleUpdate] = {}
    rejected: dict[SourceRole, Union[DocumentRejected, IdentityMismatch]] = {}
    awaiting: list[SourceRole] = []

    for role in UPDATED_ROLES:
        status = probe.statuses.get(role)
        if isinstance(status, Changed):
            updates[role] = RoleUpdate(role, status.candidate, status.candidate_hash, status.members_to_update, None)
        elif isinstance(status, OlderOnShare):
            updates[role] = RoleUpdate(role, status.candidate, status.candidate_hash, status.members_to_update, status.reason)
        elif isinstance(status, NeedsChoice):
            pick = choices.get(role)
            if pick is None:
                awaiting.append(role)
                continue
            picked = _pick_update(role, status, pick, probe, source_root, read_hash)
            if isinstance(picked, DocumentRejected):
                rejected[role] = picked
            elif picked is not None:
                updates[role] = picked

    bom_diff = None
    traveler_diff = None
    if SourceRole.BOM in updates:
        reading = _read_bom(updates[SourceRole.BOM].candidate.path, parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            rejected[SourceRole.BOM] = reading
        else:
            bom_diff = diff_bom(snapshot.held, reading)
    if SourceRole.NOTES in updates:
        reading = _read_eco(updates[SourceRole.NOTES].candidate.path, parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            rejected[SourceRole.NOTES] = reading
    if SourceRole.TRAVELER in updates:
        reading = _read_traveler(updates[SourceRole.TRAVELER].candidate.path, parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            rejected[SourceRole.TRAVELER] = reading
        else:
            traveler_diff = diff_traveler(
                snapshot.held, reading, parsers.coord_map.identity_mapping.quantity_field,
                snapshot.family_quantity,
            )

    for role in rejected:
        updates.pop(role, None)

    placements_unavailable = _placements_update(probe, choices, source_root, read_hash, updates)

    document_updates = {role: u for role, u in updates.items() if role in UPDATED_ROLES}
    requirement_diff = None
    if requirements is not None and document_updates:
        try:
            requirement_diff = requirements(probe, {role: u.candidate.path for role, u in document_updates.items()})
        except Exception as error:
            logger.warning(
                "Requirement preview failed for audits %s: %s", list(snapshot.family), _describe(error)
            )

    return FamilyPreview(
        probe=probe,
        updates=updates,
        bom=bom_diff,
        traveler=traveler_diff,
        requirements=requirement_diff,
        ops_per_board_review=SourceRole.BOM in updates and snapshot.held.ops_per_board_min is not None,
        rejected=rejected,
        awaiting_choice=tuple(awaiting),
        placements_unavailable=placements_unavailable,
    )


def _placements_update(
    probe: FamilyProbe,
    choices: Mapping[SourceRole, pathlib.Path],
    source_root: pathlib.Path,
    read_hash: HashReader,
    updates: dict[SourceRole, RoleUpdate],
) -> str | None:
    """Phase 57 §8.3: the PLACEMENTS update, with no parse; returns why none is offered, if it failed.

    post: PlacementsChanged becomes a RoleUpdate; a PlacementsNeedChoice with a pick is re-checked
          inside source_root and hashed, its members decided by decide_placements' rule; neither an
          unpicked choice nor a failure ever enters rejected or awaiting_choice
    """
    from cockpit.services import placement_updates

    status = probe.placements
    if isinstance(status, placement_updates.PlacementsChanged):
        updates[SourceRole.PLACEMENTS] = RoleUpdate(
            SourceRole.PLACEMENTS, status.candidate, status.candidate_hash, status.members_to_update, None,
        )
        return None
    if isinstance(status, ShareReadFailure):
        return status.cause
    if isinstance(status, placement_updates.PlacementsNeedChoice):
        pick = choices.get(SourceRole.PLACEMENTS)
        if pick is None:
            return None
        picked = placement_updates.pick_placements(status, pick, probe.snapshot, source_root, read_hash)
        if isinstance(picked, RoleUpdate):
            updates[SourceRole.PLACEMENTS] = picked
            return None
        return picked
    return None


# ---------- section 5: prepare ----------

@dataclass(frozen=True)
class StagedDocument:
    role: SourceRole
    original_filename: str
    stored_path: pathlib.Path
    file_hash: str
    copied_by_this_run: bool


@dataclass(frozen=True)
class PdfCoordinateChange:
    pdf_source_file_id: int
    remove_ref_des: frozenset[str]
    add: tuple[PdfComponentCoordinate, ...]
    incomplete_reason: str | None


@dataclass(frozen=True)
class DrawingHighlightsIncomplete:
    cause: str
    designators_without_coordinates: int


@dataclass(frozen=True)
class PlacementsUnavailable:
    """Phase 57 §8.4: a .bmb that couldn't be used; the family's other roles still apply."""

    reason: str
    members: tuple[int, ...]


UpdateWarning = Union[DrawingHighlightsIncomplete, PlacementsUnavailable]


@dataclass(frozen=True)
class FamilyUpdatePlan:
    """members_to_update names UPDATED_ROLES only: placements carry their own outcome per member.

    Phase 57 §8.4: placements maps each member with placements work to its MemberPlacements;
    extracts_moved lists every extract this run moved into place, once; expected_placements holds,
    per member in placements, the rows prepare read (PlacementsBasis), for the optimistic check.
    """

    key: FamilyKey
    family: tuple[int, ...]
    staged: tuple[StagedDocument, ...]
    members_to_update: Mapping[SourceRole, tuple[int, ...]]
    bom_items: tuple[BomItem, ...] | None
    traveler_metadata: Mapping[str, Any] | None
    quantity_on_traveler: int | None
    family_quantity: int
    pdf_coordinates: Mapping[int, PdfCoordinateChange]
    expected_stored_hashes: Mapping[int, Mapping[SourceRole, str]]
    warnings: tuple[UpdateWarning, ...]
    placements: Mapping[int, Any] = field(default_factory=dict)
    extracts_moved: tuple[Any, ...] = ()
    expected_placements: Mapping[int, Any] = field(default_factory=dict)

    def copies_moved(self) -> tuple[StagedDocument, ...]:
        """Every file this run moved into place: staged documents and extracts alike."""
        return (
            tuple(doc for doc in self.staged if doc.copied_by_this_run)
            + tuple(extract.as_staged_document() for extract in self.extracts_moved)
        )


@dataclass(frozen=True)
class PrepareFailed:
    failure: FamilyUpdateFailure
    copied_by_this_run: tuple[StagedDocument, ...]


CopyFile = Callable[[pathlib.Path, pathlib.Path], Any]


def _remove_quietly(path: pathlib.Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("A temporary copy could not be removed; the startup sweep will: %s", _describe(error))


def _stage(
    update: RoleUpdate,
    destination_dir: pathlib.Path,
    source_root: pathlib.Path,
    stop: threading.Event,
    run_id: str,
    copy_file: CopyFile,
) -> StagedDocument | FamilyUpdateFailure:
    """Copies one candidate into managed storage, never leaving a partial file at its destination.

    post: on a StagedDocument the file at stored_path hashes to the candidate's
          hash; on any failure no temporary file of this run remains, and
          nothing was moved into place
    """
    try:
        assert_within_root(update.candidate.path, source_root)
    except JobDirectoryEscapesRoot:
        return DocumentRejected(update.role, "ESCAPES_ROOT")

    destination = content_addressed_path(destination_dir, update.candidate.path, update.candidate_hash)
    if destination.exists():
        try:
            stored_hash = hashing.sha256_hex(destination)
        except HashingError as error:
            return StorageFailed(_describe(error))
        if stored_hash != update.candidate_hash:
            return StorageFailed("STORED_COPY_MISMATCH")
        return StagedDocument(update.role, update.candidate.name, destination, update.candidate_hash, False)

    if stop.is_set():
        return Cancelled()
    partial = destination.with_name(f"{destination.name}.{run_id}{PARTIAL_COPY_SUFFIX}")
    try:
        copy_file(update.candidate.path, partial)
    except OSError as error:
        _remove_quietly(partial)
        return StorageFailed(_describe(error))
    if stop.is_set():
        _remove_quietly(partial)
        return Cancelled()

    try:
        copied_hash = hashing.sha256_hex(partial)
    except HashingError as error:
        _remove_quietly(partial)
        return StorageFailed(_describe(error))
    if copied_hash != update.candidate_hash:
        _remove_quietly(partial)
        return CandidateChangedSincePreview(update.role)

    if stop.is_set():
        _remove_quietly(partial)
        return Cancelled()
    try:
        os.replace(partial, destination)
    except OSError as error:
        _remove_quietly(partial)
        return StorageFailed(_describe(error))
    return StagedDocument(update.role, update.candidate.name, destination, update.candidate_hash, True)


def _coordinate_changes(
    bom_items: Sequence[BomItem],
    snapshot: FamilySnapshot,
    members: Sequence[int],
    layout_parser: Any,
    dni_designators: frozenset[str] = frozenset(),
) -> tuple[dict[int, PdfCoordinateChange], tuple[UpdateWarning, ...]]:
    """Section 5.2: each member's drawing is read only when its designators changed.

    Phase 57 §4: the added designators are split against every revised designator and the revised
    BOM's DNI designators, so a merged span with an already-held neighbour still splits.
    """
    revised = frozenset().union(*(item.ref_des_list for item in bom_items))
    split_targets = set(revised) | set(dni_designators)
    member_by_id = {member.audit_id: member for member in snapshot.members}

    work: dict[int, tuple[StoredDocument, frozenset[str], frozenset[str]]] = {}
    for audit_id in members:
        drawing = snapshot.stored.get(audit_id, {}).get(SourceRole.PDF)
        if drawing is None:
            continue
        held = member_by_id[audit_id].designators
        removed, added = held - revised, revised - held
        if removed or added:
            work[audit_id] = (drawing, removed, added)

    targets_by_drawing: dict[pathlib.Path, set[str]] = collections.defaultdict(set)
    for drawing, _removed, added in work.values():
        targets_by_drawing[drawing.stored_path] |= added

    found: dict[pathlib.Path, dict[str, list[PdfComponentCoordinate]]] = {}
    unreadable: dict[pathlib.Path, str] = {}
    for drawing_path, targets in targets_by_drawing.items():
        if not targets:
            continue
        try:
            layout = layout_parser.parse(drawing_path, set(targets), split_targets=split_targets)
        except Exception as error:
            unreadable[drawing_path] = _describe(error)
            continue
        by_designator: dict[str, list[PdfComponentCoordinate]] = collections.defaultdict(list)
        for coordinate in layout.coordinates:
            by_designator[coordinate.ref_des].append(coordinate)
        found[drawing_path] = by_designator

    changes: dict[int, PdfCoordinateChange] = {}
    without_coordinates = 0
    causes: list[str] = []
    for audit_id, (drawing, removed, added) in work.items():
        cause = unreadable.get(drawing.stored_path)
        coordinates = found.get(drawing.stored_path, {})
        if cause is not None and added:
            without_coordinates += len(added)
            causes.append(cause)
        changes[audit_id] = PdfCoordinateChange(
            pdf_source_file_id=drawing.source_file_id,
            remove_ref_des=removed,
            add=tuple(c for designator in sorted(added) for c in coordinates.get(designator, [])),
            incomplete_reason=cause if added else None,
        )

    warnings: tuple[UpdateWarning, ...] = ()
    if causes:
        warnings = (DrawingHighlightsIncomplete(causes[0], without_coordinates),)
    return changes, warnings


def prepare_family_update(
    preview: FamilyPreview,
    storage_root: pathlib.Path,
    source_root: pathlib.Path,
    parsers: ParserRegistry,
    stop: threading.Event,
    run_id: str,
    copy_file: CopyFile = shutil.copy2,
    share_call: Any = None,
) -> FamilyUpdatePlan | PrepareFailed:
    """Prepares one confirmed family's update. Copies files; writes no rows; never reads the database.

    pre:  runs on the update worker; preview.can_apply, and you confirmed it
    post: on a plan every staged file is under storage_root at its
          content-addressed path and hashes to its candidate's hash; the BOM,
          ECO and traveler were re-parsed from the stored copies and passed
          every gate and identity check; pdf_coordinates is filled per section
          5.2, except for members whose drawing placements re-read whole;
          placements, extracts_moved and expected_placements are filled per
          Phase 57 §8.4, and a placements failure only adds a warning; on
          PrepareFailed no temporary file of this run remains, and
          copied_by_this_run lists every file this run moved into place,
          extracts included, for the UI thread to discard; stop seen before a
          copy, before a move into place, between placements steps, or before
          returning gives Cancelled
    raises: ValueError when the preview can't be applied, which the dialog
            prevents
    """
    from cockpit.services import placement_updates

    if not preview.can_apply:
        raise ValueError("prepare_family_update needs a preview that can be applied")

    snapshot = preview.probe.snapshot
    destination_dir = storage_root / snapshot.key.part_number / UNSPLIT_DIRECTORY
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return PrepareFailed(StorageFailed(_describe(error)), ())

    staged: list[StagedDocument] = []
    extracts_moved: list[Any] = []

    def failed(failure: FamilyUpdateFailure) -> PrepareFailed:
        return PrepareFailed(
            failure,
            tuple(doc for doc in staged if doc.copied_by_this_run)
            + tuple(extract.as_staged_document() for extract in extracts_moved),
        )

    for role in UPDATED_ROLES:
        update = preview.updates.get(role)
        if update is None:
            continue
        if stop.is_set():
            return failed(Cancelled())
        outcome = _stage(update, destination_dir, source_root, stop, run_id, copy_file)
        if not isinstance(outcome, StagedDocument):
            return failed(outcome)
        staged.append(outcome)

    stored_by_role = {doc.role: doc.stored_path for doc in staged}
    bom_items = None
    revised_bom: BomResult | None = None
    bom_dni_designators: frozenset[str] = frozenset()
    traveler_metadata = None
    quantity_on_traveler = None
    if SourceRole.BOM in stored_by_role:
        reading = _read_bom(stored_by_role[SourceRole.BOM], parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            return failed(reading)
        revised_bom = reading
        bom_items = tuple(reading.items)
        bom_dni_designators = frozenset(getattr(reading, "dni_designators", ()))
    if SourceRole.NOTES in stored_by_role:
        reading = _read_eco(stored_by_role[SourceRole.NOTES], parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            return failed(reading)
    if SourceRole.TRAVELER in stored_by_role:
        reading = _read_traveler(stored_by_role[SourceRole.TRAVELER], parsers, snapshot.key)
        if isinstance(reading, (DocumentRejected, IdentityMismatch)):
            return failed(reading)
        traveler_metadata = reading.metadata
        quantity_on_traveler = reading.quantity

    members_to_update = {
        role: update.members_to_update for role, update in preview.updates.items() if role in UPDATED_ROLES
    }

    if stop.is_set():
        return failed(Cancelled())
    family_placements = placement_updates.prepare_family_placements(
        preview, destination_dir, source_root, parsers, revised_bom, stop, run_id,
        share_call=share_call,
    )
    extracts_moved.extend(family_placements.extracts_moved)
    if family_placements.cancelled:
        return failed(Cancelled())

    pdf_coordinates: dict[int, PdfCoordinateChange] = {}
    warnings: tuple[UpdateWarning, ...] = ()
    if bom_items is not None:
        pdf_coordinates, warnings = _coordinate_changes(
            bom_items, snapshot,
            [m for m in members_to_update[SourceRole.BOM] if m not in family_placements.relabelled],
            parsers.pdf_layout_parser, bom_dni_designators,
        )
    warnings = placement_updates.merge_warnings(warnings, family_placements.warnings)

    if stop.is_set():
        return failed(Cancelled())

    expected: dict[int, dict[SourceRole, str]] = {}
    for role, members in members_to_update.items():
        for audit_id in members:
            expected.setdefault(audit_id, {})[role] = snapshot.stored[audit_id][role].file_hash

    return FamilyUpdatePlan(
        key=snapshot.key,
        family=snapshot.family,
        staged=tuple(staged),
        members_to_update=members_to_update,
        bom_items=bom_items,
        traveler_metadata=traveler_metadata,
        quantity_on_traveler=quantity_on_traveler,
        family_quantity=snapshot.family_quantity,
        pdf_coordinates=pdf_coordinates,
        expected_stored_hashes=expected,
        warnings=warnings,
        placements=family_placements.outcomes,
        extracts_moved=tuple(extracts_moved),
        expected_placements=family_placements.basis,
    )


# ---------- section 6: apply, on the UI thread ----------

@dataclass(frozen=True)
class FamilyUpdated:
    key: FamilyKey
    family: tuple[int, ...]
    replaced: tuple[SourceRole, ...]
    reaped_paths: tuple[pathlib.Path, ...]
    warnings: tuple[UpdateWarning, ...]
    quantity_not_applied: tuple[int, int] | None
    # Phase 57 §8.5: one PlacementsSummary per member that had placements work.
    placements: Mapping[int, Any] = field(default_factory=dict)


RequirementsRefresh = Callable[[Sequence[int]], None]


def discard_copies(
    copies: Sequence[StagedDocument],
    source_files: SourceFileRepository,
) -> tuple[pathlib.Path, ...]:
    """Unlinks this run's copies that no row references.

    pre:  UI thread
    post: every copy this run moved into place whose hash no source_files row
          references is gone; a copy a row does reference (an ingest meanwhile
          may have registered the same file) is kept; an unlink that fails is
          logged and left to the startup sweep; returns the paths removed
    """
    removed: list[pathlib.Path] = []
    for copy in copies:
        if not copy.copied_by_this_run:
            continue
        try:
            if source_files.reference_count(copy.file_hash) == 0:
                copy.stored_path.unlink(missing_ok=True)
                removed.append(copy.stored_path)
        except Exception as error:
            logger.warning("A staged copy could not be removed; the startup sweep will: %s", _describe(error))
    return tuple(removed)


def _family_changed(plan: FamilyUpdatePlan, repositories: FamilyUpdateRepositories) -> bool:
    current = repositories.audits.list_family(plan.key.part_number, plan.key.work_order_ref)
    if sorted(audit.id for audit in current) != sorted(plan.family):
        return True
    for audit_id, hashes in plan.expected_stored_hashes.items():
        for role, expected_hash in hashes.items():
            row = repositories.source_files.find_by_audit_and_category(audit_id, CATEGORY_OF_ROLE[role])
            if row is None or row.file_hash != expected_hash:
                return True
    for audit_id, change in plan.pdf_coordinates.items():
        row = repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
        if row is None or row.id != change.pdf_source_file_id:
            return True
    # Phase 57 §8.6: the Placements, drawing and BOM rows placements work was prepared from, by id
    # and hash, since SQLite reuses a deleted row's id.
    for audit_id, basis in plan.expected_placements.items():
        for category, expected in (
            (SourceFileCategory.PLACEMENTS, basis.placements_row),
            (SourceFileCategory.PDF, basis.drawing_row),
            (SourceFileCategory.BOM, basis.bom_row),
        ):
            row = repositories.source_files.find_by_audit_and_category(audit_id, category)
            if ((row.id, row.file_hash) if row is not None else None) != expected:
                return True
    return False


def _write_family(plan: FamilyUpdatePlan, repositories: FamilyUpdateRepositories, runtime_calc: Any) -> None:
    staged_by_role = {doc.role: doc for doc in plan.staged}
    touched: set[int] = set()
    for role in UPDATED_ROLES:
        staged = staged_by_role.get(role)
        if staged is None:
            continue
        category = CATEGORY_OF_ROLE[role]
        for audit_id in plan.members_to_update[role]:
            row = repositories.source_files.replace(audit_id, category, SourceFileDraft(
                audit_id=audit_id, file_category=category,
                original_filename=staged.original_filename,
                local_storage_path=staged.stored_path, file_hash=staged.file_hash,
            ))
            touched.add(audit_id)
            if role == SourceRole.BOM:
                _insert_bom_rows(audit_id, row, plan.bom_items or (), repositories)
            elif role == SourceRole.TRAVELER:
                repositories.audits.set_traveler_metadata(audit_id, dict(plan.traveler_metadata or {}))

    for audit_id, change in plan.pdf_coordinates.items():
        repositories.pdf_coords.delete_ref_des(change.pdf_source_file_id, change.remove_ref_des)
        if change.add:
            repositories.pdf_coords.bulk_insert([
                PdfComponentCoordDraft(
                    source_file_id=change.pdf_source_file_id, ref_des=c.ref_des,
                    page_index=c.page_index, x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2,
                )
                for c in change.add
            ])

    if runtime_calc is not None:
        for audit_id in sorted(touched):
            runtime_calc.persist(audit_id)


def _insert_bom_rows(
    audit_id: int,
    bom_row: SourceFile,
    bom_items: Sequence[BomItem],
    repositories: FamilyUpdateRepositories,
) -> None:
    """Ingest's construction of the component and THT rows for a new BOM row."""
    components = [
        AuditBomComponentDraft(
            source_file_id=bom_row.id, component_mpn=item.component_mpn, ref_des=designator,
            mount_type=item.mount_type, description=item.description, find_number=item.find_number,
        )
        for item in bom_items
        for designator in item.ref_des_list
    ]
    if components:
        repositories.bom_components.bulk_insert(components)
    through_hole = [
        ThtChecklistItemDraft(
            audit_id=audit_id, source_file_id=bom_row.id,
            component_mpn=item.component_mpn, description=item.description,
        )
        for item in bom_items
        if item.mount_type == "T"
    ]
    if through_hole:
        repositories.tht_checklist.insert_many(through_hole)


def apply_family_update(
    plan: FamilyUpdatePlan,
    repositories: FamilyUpdateRepositories,
    runtime_calc: Any,
    requirements_refresh: RequirementsRefresh | None = None,
) -> FamilyUpdated | FamilyUpdateFailure:
    """Applies a prepared plan to the whole family in one savepoint.

    pre:  called on the thread that owns the connection
    post: on FamilyUpdated, for every member a role names: its source_files row
          for the role is replaced; BOM components and THT items rebuilt when
          the BOM changed; drawing coordinates changed per the plan;
          traveler_metadata replaced, with is_class_3 and is_clean_process
          recomputed, when the traveler changed; runtimes recomputed; operator
          state untouched; then, Phase 57 §8.5, each member's placements in a
          savepoint of its own, a failure rolling back that member alone; after
          the last, extracts no row references are discarded; superseded files
          whose reference_count is 0 are reaped and requirements_refresh, when
          given, is called once with the family; on a failure the family is
          exactly as it was and this run's unreferenced copies are discarded
    raises: nothing from requirements_refresh, which is best-effort
    """
    from cockpit.services import placement_updates

    copies = plan.copies_moved()
    if _family_changed(plan, repositories):
        discard_copies(copies, repositories.source_files)
        return FamilyChangedSincePreview(plan.family)

    superseded: list[SourceFile] = []
    seen_paths: set[pathlib.Path] = set()
    for role, members in plan.members_to_update.items():
        for audit_id in members:
            row = repositories.source_files.find_by_audit_and_category(audit_id, CATEGORY_OF_ROLE[role])
            if row is not None and row.local_storage_path not in seen_paths:
                seen_paths.add(row.local_storage_path)
                superseded.append(row)
    for row in placement_updates.superseded_extract_rows(plan, repositories):
        if row.local_storage_path not in seen_paths:
            seen_paths.add(row.local_storage_path)
            superseded.append(row)

    conn = repositories.conn
    conn.execute("SAVEPOINT update_family")
    try:
        _write_family(plan, repositories, runtime_calc)
        conn.execute("RELEASE SAVEPOINT update_family")
    except Exception as error:
        conn.execute("ROLLBACK TO SAVEPOINT update_family")
        conn.execute("RELEASE SAVEPOINT update_family")
        logger.warning("Document update rolled back for audits %s: %s", list(plan.family), _describe(error))
        discard_copies(copies, repositories.source_files)
        return WriteFailed(_describe(error))

    placements = placement_updates.apply_family_placements(plan, repositories, runtime_calc)
    discard_copies(tuple(extract.as_staged_document() for extract in plan.extracts_moved), repositories.source_files)

    reaped: tuple[pathlib.Path, ...] = ()
    try:
        report = StorageReaper(repositories.source_files).reap(superseded)
        reaped = tuple(report.deleted_paths)
        if report.failed_paths:
            logger.warning(
                "%d superseded file(s) could not be removed; the startup sweep will retry",
                len(report.failed_paths),
            )
    except Exception as error:
        logger.warning("Reaping superseded files failed: %s", _describe(error))

    if requirements_refresh is not None:
        try:
            requirements_refresh(plan.family)
        except Exception as error:
            logger.warning(
                "Requirements refresh request failed for audits %s; the next pass covers them: %s",
                list(plan.family), _describe(error),
            )

    quantity_not_applied = None
    if plan.quantity_on_traveler is not None and plan.quantity_on_traveler != plan.family_quantity:
        quantity_not_applied = (plan.quantity_on_traveler, plan.family_quantity)

    return FamilyUpdated(
        key=plan.key,
        family=plan.family,
        replaced=tuple(role for role in UPDATED_ROLES if role in plan.members_to_update),
        reaped_paths=reaped,
        warnings=plan.warnings,
        quantity_not_applied=quantity_not_applied,
        placements=placements,
    )


# ---------- reading the database, on the UI thread ----------

def choose_reference_member(family: Sequence[ActiveAudit]) -> int:
    """The member whose documents are the baseline: the unsuffixed one, else the lowest id.

    pre:  family is not empty
    """
    unsuffixed = [audit.id for audit in family if audit.split_suffix == ""]
    if unsuffixed:
        return min(unsuffixed)
    return min(audit.id for audit in family)


def _stored_document(role: SourceRole, row: SourceFile) -> StoredDocument:
    try:
        stat = row.local_storage_path.stat()
        size, modified_at_ns = stat.st_size, stat.st_mtime_ns
    except OSError:
        size, modified_at_ns = None, None
    return StoredDocument(
        role=role, source_file_id=row.id, original_filename=row.original_filename,
        stored_path=row.local_storage_path, file_hash=row.file_hash,
        size=size, modified_at_ns=modified_at_ns,
        source_hash=row.source_hash, source_size=row.source_size,
        source_modified_ns=row.source_modified_ns,
    )


def snapshot_family(key: FamilyKey, repositories: FamilyUpdateRepositories) -> FamilySnapshot | None:
    """Reads, as values, everything the worker needs about one family.

    pre:  UI thread
    post: None when the family no longer exists; otherwise every member's
          stored documents (the drawing and the Placements extract included,
          for section 5.2 and Phase 57 §8), its designators, its recorded .bmb
          rejections, and the reference member's rows the preview diffs against
    """
    family = repositories.audits.list_family(key.part_number, key.work_order_ref)
    if not family:
        return None
    reference_id = choose_reference_member(family)

    stored: dict[int, dict[SourceRole, StoredDocument]] = {}
    components_by_member: dict[int, list[AuditBomComponent]] = {}
    rejections: dict[int, tuple[PlacementRejection, ...]] = {}
    for audit in family:
        rejections[audit.id] = tuple(repositories.placement_rejections.list_for_audit(audit.id))
        documents: dict[SourceRole, StoredDocument] = {}
        for row in repositories.source_files.list_for_audit(audit.id):
            role = ROLE_OF_CATEGORY.get(SourceFileCategory(row.file_category))
            if role is not None:
                documents[role] = _stored_document(role, row)
        stored[audit.id] = documents
        bom = documents.get(SourceRole.BOM)
        components_by_member[audit.id] = (
            repositories.bom_components.list_for_source_file(bom.source_file_id) if bom else []
        )

    members = tuple(
        FamilyMember(
            audit_id=audit.id, split_suffix=audit.split_suffix, quantity=audit.quantity,
            designators=frozenset(c.ref_des for c in components_by_member[audit.id]),
        )
        for audit in family
    )
    reference = next(audit for audit in family if audit.id == reference_id)
    held = HeldState(
        components=tuple(components_by_member[reference_id]),
        tht_items=tuple(
            (item.component_mpn, item.description)
            for item in repositories.tht_checklist.list_for_audit(reference_id)
        ),
        traveler_metadata=reference.traveler_metadata,
        ops_per_board_min=reference.ops_per_board_min,
    )
    return FamilySnapshot(
        key=key, members=members, reference_member=reference_id,
        article_revision=reference.article_revision or FIRST_ARTICLE,
        stored=stored, held=held, rejections=rejections,
    )


def open_family_keys(audits: AuditRepository) -> list[FamilyKey]:
    """Every open family once, in part-number order."""
    return sorted({FamilyKey(audit.part_number, audit.work_order_ref) for audit in audits.list_open()})
