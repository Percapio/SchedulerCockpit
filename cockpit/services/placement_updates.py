"""Placements through Phase 55's updates (Architecture/20261115-Phase57.md §8).

document_updates calls this module at four seams:
- the probe: decide_placements, on the update worker;
- the preview: pick_placements, on the update worker;
- prepare: prepare_family_placements, on the update worker; it reads files and writes no rows;
- apply: apply_family_placements, on the UI thread, one savepoint per member after the family's.

A placements failure never fails the family: it becomes a member's outcome and a warning.
"""

from __future__ import annotations

import dataclasses
import logging
import pathlib
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, Sequence, Union

from cockpit.ingestion.errors import JobDirectoryEscapesRoot
from cockpit.ingestion.locator import assert_within_root
from cockpit.ingestion.parsers.results import BomResult
from cockpit.ingestion.roles import SourceRole
from cockpit.layout.results import PdfComponentCoordinate
from cockpit.persistence.clock import utcnow
from cockpit.persistence.types import (
    PdfComponentCoordDraft,
    PlacementRejection,
    SourceFile,
    SourceFileCategory,
    SourceFileDraft,
)
from cockpit.placements.admission import BomDisagrees, admit_placements
from cockpit.placements.bmb_file import (
    BmbPlacement,
    BmbProject,
    DecoderUnavailable,
    ExtractCorrupt,
    Extracted,
    ExtractMissing,
    ExtractUnreadable,
    Rejected,
    ShareReadFailed,
    Stopped,
    direct_share_call,
    extract_bmb_sections,
    load_stored_extract,
    read_bmb_project,
)
from cockpit.placements.bmb_file import StorageFailed as ExtractStorageFailed
from cockpit.placements.registration import (
    DrawingLabel,
    PageOutcome,
    Registered,
    RegistrationStopped,
    register_drawing_pages,
)
from cockpit.placements.wording import reason_text
from cockpit.protocols import ParserRegistry
from cockpit.services.document_updates import (
    CandidateFile,
    Cancelled,
    DrawingHighlightsIncomplete,
    FamilyPreview,
    FamilySnapshot,
    FamilyUpdatePlan,
    FamilyUpdateRepositories,
    HashReader,
    PlacementsUnavailable,
    RoleUpdate,
    ShareReadFailure,
    StagedDocument,
    StoredDocument,
    UpdateWarning,
    WriteFailed,
    _describe,
)

logger = logging.getLogger(__name__)


# ---------- the probe (§8.2) ----------

@dataclass(frozen=True)
class NoBmb:
    pass


@dataclass(frozen=True)
class PlacementsUnchanged:
    pass


@dataclass(frozen=True)
class PlacementsNeedChoice:
    candidates: tuple[CandidateFile, ...]


@dataclass(frozen=True)
class PlacementsChanged:
    candidate: CandidateFile
    candidate_hash: str
    members_to_update: tuple[int, ...]


PlacementsStatus = Union[NoBmb, PlacementsUnchanged, PlacementsNeedChoice, PlacementsChanged]


def _stored(snapshot: FamilySnapshot, audit_id: int, role: SourceRole) -> StoredDocument | None:
    return snapshot.stored.get(audit_id, {}).get(role)


def standing_rejections(snapshot: FamilySnapshot, audit_id: int) -> tuple[PlacementRejection, ...]:
    """A member's rejections that still stand: a file's own, or a refusal measured against its current BOM."""
    bom = _stored(snapshot, audit_id, SourceRole.BOM)
    current = bom.file_hash if bom is not None else None
    return tuple(
        rejection for rejection in snapshot.rejections.get(audit_id, ())
        if rejection.bom_hash is None or rejection.bom_hash == current
    )


def _decided_without_hash(snapshot: FamilySnapshot, audit_id: int, candidate: CandidateFile) -> bool:
    seen = (candidate.size, candidate.modified_at_ns)
    row = _stored(snapshot, audit_id, SourceRole.PLACEMENTS)
    if row is not None and (row.source_size, row.source_modified_ns) == seen:
        return True
    return any((r.share_size, r.share_modified_ns) == seen for r in standing_rejections(snapshot, audit_id))


def _needs(snapshot: FamilySnapshot, audit_id: int, candidate_hash: str) -> bool:
    row = _stored(snapshot, audit_id, SourceRole.PLACEMENTS)
    if row is not None and row.source_hash == candidate_hash:
        return False
    return not any(r.share_hash == candidate_hash for r in standing_rejections(snapshot, audit_id))


def members_needing(snapshot: FamilySnapshot, candidate_hash: str) -> tuple[int, ...]:
    """The members that need a .bmb whose share hash is candidate_hash, by §8.2's rule."""
    return tuple(sorted(m.audit_id for m in snapshot.members if _needs(snapshot, m.audit_id, candidate_hash)))


def decide_placements(
    candidates: Sequence[CandidateFile],
    snapshot: FamilySnapshot,
    read_hash: HashReader,
) -> PlacementsStatus | ShareReadFailure:
    """Decides whether the job root's .bmb needs reading, member by member.

    pre:  runs on the update worker; candidates are the job root's .bmb files
    post: NoBmb without candidates; PlacementsNeedChoice with more than one; otherwise a member
          needs the file iff it holds no Placements row or its row's source_hash differs from the
          candidate's, and that hash isn't a standing rejection of the member's; a member is decided
          without the hash when the candidate's size and modified time equal its row's, or a
          standing rejection's; the candidate is read, once, iff some member can't be decided that
          way; PlacementsChanged lists the members that need it
    """
    if not candidates:
        return NoBmb()
    if len(candidates) > 1:
        return PlacementsNeedChoice(tuple(sorted(candidates, key=lambda c: c.name)))
    candidate = candidates[0]
    undecided = [m.audit_id for m in snapshot.members if not _decided_without_hash(snapshot, m.audit_id, candidate)]
    if not undecided:
        return PlacementsUnchanged()
    candidate_hash = read_hash(candidate)
    if isinstance(candidate_hash, ShareReadFailure):
        return candidate_hash
    needing = tuple(sorted(a for a in undecided if _needs(snapshot, a, candidate_hash)))
    if not needing:
        return PlacementsUnchanged()
    return PlacementsChanged(candidate, candidate_hash, needing)


def pick_placements(
    status: PlacementsNeedChoice,
    pick: pathlib.Path,
    snapshot: FamilySnapshot,
    source_root: pathlib.Path,
    read_hash: HashReader,
    share_call: Any = None,
) -> RoleUpdate | str | None:
    """Turns your pick between .bmb files into an update (§8.3); a failure is its reason, never a block.

    post: the pick is re-checked inside source_root and hashed, as _pick_update does, each through
          share_call (Phase 55b §4.4); the members are decided by decide_placements' rule against that
          hash; None when no member needs it
    """
    guard = share_call or direct_share_call
    candidate = next((c for c in status.candidates if c.path == pick), None)
    if candidate is None:
        return "PICK_NOT_A_CANDIDATE"
    try:
        guard(lambda: assert_within_root(candidate.path, source_root))
    except JobDirectoryEscapesRoot:
        return "ESCAPES_ROOT"
    candidate_hash = guard(lambda: read_hash(candidate))
    if isinstance(candidate_hash, ShareReadFailure):
        return candidate_hash.cause
    needing = members_needing(snapshot, candidate_hash)
    if not needing:
        return None
    return RoleUpdate(SourceRole.PLACEMENTS, candidate, candidate_hash, needing, None)


# ---------- prepare (§8.4) ----------

@dataclass(frozen=True)
class StagedExtract:
    original_filename: str
    stored_path: pathlib.Path
    file_hash: str
    source_hash: str
    source_size: int
    source_modified_ns: int
    moved_by_this_run: bool

    def as_staged_document(self) -> StagedDocument:
        """The form discard_copies takes: it unlinks only files no row references."""
        return StagedDocument(
            SourceRole.PLACEMENTS, self.original_filename, self.stored_path, self.file_hash, self.moved_by_this_run,
        )


@dataclass(frozen=True)
class NewFile:
    candidate: CandidateFile
    candidate_hash: str


@dataclass(frozen=True)
class Readmit:
    row: StoredDocument


PlacementsWork = Union[NewFile, Readmit]


@dataclass(frozen=True)
class DrawingLabels:
    drawing_source_file_id: int
    labels: tuple[PdfComponentCoordinate, ...]


@dataclass(frozen=True)
class PlacementsWritten:
    """new_extract is set when the member's Placements row is replaced; drawing when its labels were re-read."""

    new_extract: StagedExtract | None
    admitted: tuple[BmbPlacement, ...]
    drawing: DrawingLabels | None
    pages: tuple[PageOutcome, ...]
    bom_designators: int
    drawing_unreadable: str | None = None


@dataclass(frozen=True)
class PlacementsRemoved:
    refusal: PlacementRejection


@dataclass(frozen=True)
class PlacementsRejected:
    rejection: PlacementRejection


@dataclass(frozen=True)
class PlacementsKept:
    """Nothing changes. after_bom_change says the stored placements weren't re-checked against a revised BOM."""

    reason: str
    after_bom_change: bool


MemberPlacements = Union[PlacementsWritten, PlacementsRemoved, PlacementsRejected, PlacementsKept]


RowIdentity = tuple[int, str]


@dataclass(frozen=True)
class PlacementsBasis:
    """The rows a member's placements work was prepared from (§8.6), each as (id, file_hash); None
    where the member had none. The hash matters: SQLite reuses the highest rowid once that row is
    deleted, so a replaced row can come back under the same id."""

    placements_row: RowIdentity | None
    drawing_row: RowIdentity | None
    bom_row: RowIdentity | None


@dataclass(frozen=True)
class MemberPlacementsInputs:
    audit_id: int
    work: PlacementsWork
    designators: frozenset[str]
    dni_designators: frozenset[str]
    bom_hash: str | None
    drawing: StoredDocument | None
    bom_changes: bool


@dataclass(frozen=True)
class FamilyPlacements:
    outcomes: Mapping[int, MemberPlacements]
    extracts_moved: tuple[StagedExtract, ...]
    warnings: tuple[UpdateWarning, ...]
    basis: Mapping[int, PlacementsBasis]
    relabelled: frozenset[int]
    cancelled: bool = False


@dataclass(frozen=True)
class _ExtractFailure:
    reason: str
    rejection: Any = None


class _Extracts:
    """Extracts and projects for one family's run: each share file is extracted once, each extract read once."""

    def __init__(
        self,
        destination_dir: pathlib.Path,
        source_root: pathlib.Path,
        run_id: str,
        share_call: Any,
        stop: Any,
        candidates: tuple[CandidateFile, ...],
    ) -> None:
        self.destination_dir = destination_dir
        self.source_root = source_root
        self.run_id = run_id
        self.share_call = share_call
        self.stop = stop
        self.candidates = candidates
        self.moved: list[StagedExtract] = []
        self._by_candidate: dict[tuple[pathlib.Path, str], StagedExtract | _ExtractFailure] = {}
        self._projects: dict[str, Any] = {}

    def extract(self, candidate: CandidateFile, expected_source_hash: str) -> StagedExtract | _ExtractFailure | Cancelled:
        key = (candidate.path, expected_source_hash)
        if key not in self._by_candidate:
            outcome = self._extract(candidate, expected_source_hash)
            if isinstance(outcome, Cancelled):
                return outcome
            self._by_candidate[key] = outcome
        return self._by_candidate[key]

    def _extract(self, candidate: CandidateFile, expected_source_hash: str) -> StagedExtract | _ExtractFailure | Cancelled:
        try:
            self.share_call(lambda: assert_within_root(candidate.path, self.source_root))
        except JobDirectoryEscapesRoot:
            return _ExtractFailure("the .bmb file is outside the Source Root")
        outcome = extract_bmb_sections(
            candidate.path, self.destination_dir, self.run_id, share_call=self.share_call, stop=self.stop,
        )
        if isinstance(outcome, Stopped):
            return Cancelled()
        if isinstance(outcome, Rejected):
            return _ExtractFailure(reason_text(outcome.reason), rejection=outcome.reason)
        if isinstance(outcome, ShareReadFailed):
            return _ExtractFailure(f"the .bmb file couldn't be read ({outcome.cause})")
        if isinstance(outcome, ExtractStorageFailed):
            return _ExtractFailure(f"the .bmb file couldn't be stored ({outcome.cause})")
        assert isinstance(outcome, Extracted)
        extract = outcome.extract
        staged = StagedExtract(
            original_filename=candidate.name, stored_path=extract.stored_path, file_hash=extract.file_hash,
            source_hash=extract.source_hash, source_size=extract.source_size,
            source_modified_ns=extract.source_modified_ns, moved_by_this_run=outcome.moved_by_this_run,
        )
        if staged.moved_by_this_run:
            self.moved.append(staged)
        if staged.source_hash != expected_source_hash:
            return _ExtractFailure("the .bmb file changed after it was checked")
        return staged

    def stored(self, row: StoredDocument) -> tuple[Any, StagedExtract | None] | _ExtractFailure | Cancelled:
        """A member's stored extract, or, when it can't be used, the share's .bmb extracted again iff it
        still hashes to the row's source_hash. Returns (extract, new_extract)."""
        loaded = load_stored_extract(SimpleNamespace(
            local_storage_path=row.stored_path, file_hash=row.file_hash, source_hash=row.source_hash,
            source_size=row.source_size, source_modified_ns=row.source_modified_ns,
        ))
        if not isinstance(loaded, (ExtractMissing, ExtractCorrupt, ExtractUnreadable)):
            return loaded, None
        what = (
            "missing" if isinstance(loaded, ExtractMissing)
            else "corrupt" if isinstance(loaded, ExtractCorrupt)
            else f"unreadable ({loaded.cause})"
        )
        if len(self.candidates) != 1:
            where = "the job folder has no .bmb file" if not self.candidates else "the job folder holds several .bmb files"
            return _ExtractFailure(f"the stored .bmb extract is {what}, and {where}")
        again = self.extract(self.candidates[0], row.source_hash)
        if isinstance(again, Cancelled):
            return again
        if isinstance(again, _ExtractFailure):
            return _ExtractFailure(f"the stored .bmb extract is {what}, and {again.reason}")
        return again, again

    def project(self, extract: Any) -> Any:
        """read_bmb_project of an extract, once per stored bytes; an unreadable file is an _ExtractFailure."""
        if extract.file_hash not in self._projects:
            try:
                self._projects[extract.file_hash] = read_bmb_project(extract)
            except OSError as error:
                self._projects[extract.file_hash] = _ExtractFailure(
                    f"the stored .bmb extract couldn't be read ({_describe(error)})"
                )
        return self._projects[extract.file_hash]


def _rejection(name: str, share_hash: str, size: int | None, modified_ns: int | None, reason: Any,
               bom_hash: str | None) -> PlacementRejection:
    return PlacementRejection(
        share_file_name=name, share_hash=share_hash, share_size=size or 0, share_modified_ns=modified_ns or 0,
        reason=reason, bom_hash=bom_hash, seen_at=utcnow(),
    )


def _drawing_labels(
    member: MemberPlacementsInputs,
    parsers: ParserRegistry,
) -> tuple[PdfComponentCoordinate, ...] | str:
    drawing = member.drawing
    assert drawing is not None
    try:
        layout = parsers.pdf_layout_parser.parse(
            drawing.stored_path, set(member.designators),
            split_targets=set(member.designators) | set(member.dni_designators),
        )
    except Exception as error:
        return _describe(error)
    return tuple(layout.coordinates)


def prepare_member_placements(
    member: MemberPlacementsInputs,
    extracts: _Extracts,
    parsers: ParserRegistry,
    stop: Any,
) -> MemberPlacements | Cancelled:
    """Prepares one member's placements from the files alone.

    pre:  runs on the update worker; member.work names the member's case (§8.4's table)
    post: the project is read and admitted against the member's designators; when the member has a
          drawing, its labels are re-read with keep_targets the designators and split_targets those
          plus the DNI designators, and its pages registered; a new file that is rejected, or whose
          admission is refused, gives PlacementsRejected; a stored extract whose re-admission is
          refused gives PlacementsRemoved; a stored extract that can't be used is extracted again
          when the share's .bmb still hashes to the row's source_hash, and otherwise gives
          PlacementsKept; a share or storage failure, and a build without the decoder, give
          PlacementsKept; a drawing that can't be read leaves drawing absent and registers nothing;
          nothing is written to the database
    """
    new_file = isinstance(member.work, NewFile)
    after_bom_change = member.bom_changes and not new_file
    if member.bom_hash is None:
        return PlacementsKept("no BOM is stored for this audit", after_bom_change)

    if isinstance(member.work, NewFile):
        got = extracts.extract(member.work.candidate, member.work.candidate_hash)
        if isinstance(got, Cancelled):
            return got
        if isinstance(got, _ExtractFailure):
            if got.rejection is not None:
                candidate = member.work.candidate
                return PlacementsRejected(_rejection(
                    candidate.name, member.work.candidate_hash, candidate.size, candidate.modified_at_ns,
                    got.rejection, None,
                ))
            return PlacementsKept(got.reason, after_bom_change)
        extract: Any = got
        new_extract: StagedExtract | None = got
        share_name, share_hash = got.original_filename, got.source_hash
        share_size, share_modified_ns = got.source_size, got.source_modified_ns
    else:
        row = member.work.row
        got = extracts.stored(row)
        if isinstance(got, Cancelled):
            return got
        if isinstance(got, _ExtractFailure):
            return PlacementsKept(got.reason, after_bom_change)
        extract, new_extract = got
        share_name, share_hash = row.original_filename, row.source_hash
        share_size, share_modified_ns = row.source_size, row.source_modified_ns

    project = extracts.project(extract)
    if isinstance(project, _ExtractFailure):
        return PlacementsKept(project.reason, after_bom_change)
    if isinstance(project, DecoderUnavailable):
        return PlacementsKept(reason_text(project), after_bom_change)
    if not isinstance(project, BmbProject):
        if new_file:
            return PlacementsRejected(_rejection(share_name, share_hash, share_size, share_modified_ns, project, None))
        return PlacementsKept(reason_text(project), after_bom_change)

    admission = admit_placements(project, member.designators)
    if isinstance(admission, BomDisagrees):
        refusal = _rejection(share_name, share_hash, share_size, share_modified_ns, admission, member.bom_hash)
        return PlacementsRejected(refusal) if new_file else PlacementsRemoved(refusal)

    drawing: DrawingLabels | None = None
    pages: tuple[PageOutcome, ...] = ()
    unreadable: str | None = None
    if member.drawing is not None:
        if stop is not None and stop.is_set():
            return Cancelled()
        labels = _drawing_labels(member, parsers)
        if isinstance(labels, str):
            unreadable = labels
        else:
            drawing = DrawingLabels(member.drawing.source_file_id, labels)
            outcome = register_drawing_pages(
                admission.admitted,
                [DrawingLabel(c.ref_des, c.page_index, (c.x1, c.y1, c.x2, c.y2), c.from_split) for c in labels],
                stop,
            )
            if isinstance(outcome, RegistrationStopped):
                return Cancelled()
            pages = tuple(outcome)

    return PlacementsWritten(
        new_extract=new_extract,
        admitted=tuple(admission.admitted.values()),
        drawing=drawing,
        pages=pages,
        bom_designators=len(member.designators),
        drawing_unreadable=unreadable,
    )


def _lapsing_refusal(
    snapshot: FamilySnapshot,
    audit_id: int,
    candidates: tuple[CandidateFile, ...],
) -> PlacementRejection | None:
    """The member's standing BomDisagrees refusal of the job's one .bmb, which the BOM change lapses.

    post: the refusal iff there is exactly one candidate and its size and modified time equal the
          refusal's; extraction checks the share hash, so a different file with equal size and time
          fails as changed rather than being admitted under the refused file's hash
    """
    if len(candidates) != 1:
        return None
    seen = (candidates[0].size, candidates[0].modified_at_ns)
    return next(
        (
            r for r in standing_rejections(snapshot, audit_id)
            if isinstance(r.reason, BomDisagrees) and r.bom_hash is not None
            and (r.share_size, r.share_modified_ns) == seen
        ),
        None,
    )


def _member_inputs(
    preview: FamilyPreview,
    parsers: ParserRegistry,
    revised_bom: BomResult | None,
) -> dict[int, MemberPlacementsInputs]:
    """§8.4's trigger: NewFile for PLACEMENTS' members; Readmit for the BOM's members holding a row."""
    snapshot = preview.probe.snapshot
    work: dict[int, PlacementsWork] = {}
    update = preview.updates.get(SourceRole.PLACEMENTS)
    if update is not None:
        for audit_id in update.members_to_update:
            work[audit_id] = NewFile(update.candidate, update.candidate_hash)

    bom_update = preview.updates.get(SourceRole.BOM)
    bom_members = frozenset(bom_update.members_to_update) if bom_update is not None and revised_bom is not None else frozenset()
    candidates = tuple(getattr(preview.probe, "placement_candidates", ()))
    for audit_id in sorted(bom_members):
        if audit_id in work:
            continue
        lapsing = _lapsing_refusal(snapshot, audit_id, candidates)
        row = _stored(snapshot, audit_id, SourceRole.PLACEMENTS)
        if lapsing is not None:
            work[audit_id] = NewFile(candidates[0], lapsing.share_hash)
        elif row is not None:
            work[audit_id] = Readmit(row)

    revised_designators: frozenset[str] = frozenset()
    revised_dni: frozenset[str] = frozenset()
    if revised_bom is not None:
        revised_designators = frozenset().union(*(item.ref_des_list for item in revised_bom.items))
        revised_dni = frozenset(getattr(revised_bom, "dni_designators", ()))

    stored_dni: dict[pathlib.Path, frozenset[str]] = {}

    def dni_of(bom: StoredDocument) -> frozenset[str]:
        if bom.stored_path not in stored_dni:
            try:
                stored_dni[bom.stored_path] = frozenset(getattr(parsers.bom_parser.parse(bom.stored_path), "dni_designators", ()))
            except Exception as error:
                logger.warning("A stored BOM couldn't be parsed for its DNI designators; splitting without them: %s",
                               _describe(error))
                stored_dni[bom.stored_path] = frozenset()
        return stored_dni[bom.stored_path]

    designators_of = {member.audit_id: member.designators for member in snapshot.members}
    inputs: dict[int, MemberPlacementsInputs] = {}
    for audit_id, member_work in sorted(work.items()):
        drawing = _stored(snapshot, audit_id, SourceRole.PDF)
        bom = _stored(snapshot, audit_id, SourceRole.BOM)
        if audit_id in bom_members:
            designators, dni, bom_hash = revised_designators, revised_dni, bom_update.candidate_hash
        elif bom is not None:
            designators = designators_of.get(audit_id, frozenset())
            dni = dni_of(bom) if drawing is not None else frozenset()
            bom_hash = bom.file_hash
        else:
            designators, dni, bom_hash = frozenset(), frozenset(), None
        inputs[audit_id] = MemberPlacementsInputs(
            audit_id=audit_id, work=member_work, designators=designators, dni_designators=dni,
            bom_hash=bom_hash, drawing=drawing, bom_changes=audit_id in bom_members,
        )
    return inputs


def _shared_key(member: MemberPlacementsInputs) -> tuple:
    """Siblings that share every input are prepared once (§8.4, audit 21)."""
    source = (
        ("new", member.work.candidate.path, member.work.candidate_hash) if isinstance(member.work, NewFile)
        else ("stored", member.work.row.file_hash, member.work.row.source_hash)
    )
    drawing_hash = member.drawing.file_hash if member.drawing is not None else None
    return source, member.designators, member.dni_designators, drawing_hash, member.bom_hash, member.bom_changes


def _for_member(outcome: MemberPlacements, member: MemberPlacementsInputs) -> MemberPlacements:
    """A sibling's shared outcome, pointed at this member's own drawing row."""
    if isinstance(outcome, PlacementsWritten) and outcome.drawing is not None and member.drawing is not None:
        return dataclasses.replace(
            outcome, drawing=DrawingLabels(member.drawing.source_file_id, outcome.drawing.labels),
        )
    return outcome


def _unavailable_reason(outcome: MemberPlacements) -> str | None:
    if isinstance(outcome, PlacementsRejected):
        return reason_text(outcome.rejection.reason)
    if isinstance(outcome, PlacementsRemoved):
        return reason_text(outcome.refusal.reason)
    if isinstance(outcome, PlacementsKept):
        return outcome.reason
    return None


def _warnings(outcomes: Mapping[int, MemberPlacements]) -> tuple[UpdateWarning, ...]:
    by_reason: dict[str, list[int]] = {}
    unreadable: list[str] = []
    for audit_id in sorted(outcomes):
        outcome = outcomes[audit_id]
        reason = _unavailable_reason(outcome)
        if reason is not None:
            by_reason.setdefault(reason, []).append(audit_id)
        if isinstance(outcome, PlacementsWritten) and outcome.drawing_unreadable is not None:
            unreadable.append(outcome.drawing_unreadable)
    warnings: list[UpdateWarning] = [
        PlacementsUnavailable(reason, tuple(members)) for reason, members in by_reason.items()
    ]
    if unreadable:
        warnings.append(DrawingHighlightsIncomplete(unreadable[0], 0))
    return tuple(warnings)


def merge_warnings(
    documents: tuple[UpdateWarning, ...],
    placements: tuple[UpdateWarning, ...],
) -> tuple[UpdateWarning, ...]:
    """Phase 55's warnings, then placements', without a second DrawingHighlightsIncomplete for one cause."""
    causes = {w.cause for w in documents if isinstance(w, DrawingHighlightsIncomplete)}
    return documents + tuple(
        w for w in placements if not (isinstance(w, DrawingHighlightsIncomplete) and w.cause in causes)
    )


def prepare_family_placements(
    preview: FamilyPreview,
    destination_dir: pathlib.Path,
    source_root: pathlib.Path,
    parsers: ParserRegistry,
    revised_bom: BomResult | None,
    stop: Any,
    run_id: str,
    share_call: Any = None,
) -> FamilyPlacements:
    """Prepares every member's placements work for one family; reads files only.

    pre:  runs on the update worker, after prepare_family_update staged and read the other roles;
          revised_bom is the staged BOM's parse when the BOM is in the update
    post: one MemberPlacements per member §8.4's trigger names; siblings sharing every input are
          prepared once and share one StagedExtract; extracts_moved lists every extract this run
          moved into place, once; basis holds the rows each member's work was read from; relabelled
          names the members whose drawing labels were re-read whole; cancelled when stop was seen
    """
    snapshot = preview.probe.snapshot
    inputs = _member_inputs(preview, parsers, revised_bom)
    extracts = _Extracts(
        destination_dir, source_root, run_id, share_call or direct_share_call, stop,
        tuple(getattr(preview.probe, "placement_candidates", ())),
    )

    def cancelled() -> FamilyPlacements:
        return FamilyPlacements({}, tuple(extracts.moved), (), {}, frozenset(), cancelled=True)

    outcomes: dict[int, MemberPlacements] = {}
    shared: dict[tuple, MemberPlacements] = {}
    for audit_id, member in inputs.items():
        if stop is not None and stop.is_set():
            return cancelled()
        key = _shared_key(member)
        if key not in shared:
            outcome = prepare_member_placements(member, extracts, parsers, stop)
            if isinstance(outcome, Cancelled):
                return cancelled()
            shared[key] = outcome
        outcomes[audit_id] = _for_member(shared[key], member)

    basis = {
        audit_id: PlacementsBasis(
            placements_row=_row_identity(snapshot, audit_id, SourceRole.PLACEMENTS),
            drawing_row=_row_identity(snapshot, audit_id, SourceRole.PDF),
            bom_row=_row_identity(snapshot, audit_id, SourceRole.BOM),
        )
        for audit_id, outcome in outcomes.items()
        if not isinstance(outcome, PlacementsKept)
    }
    relabelled = frozenset(
        audit_id for audit_id, outcome in outcomes.items()
        if isinstance(outcome, PlacementsWritten) and outcome.drawing is not None
    )
    return FamilyPlacements(outcomes, tuple(extracts.moved), _warnings(outcomes), basis, relabelled)


def _row_identity(snapshot: FamilySnapshot, audit_id: int, role: SourceRole) -> RowIdentity | None:
    document = _stored(snapshot, audit_id, role)
    return (document.source_file_id, document.file_hash) if document is not None else None


# ---------- apply (§8.5) ----------

@dataclass(frozen=True)
class Placed:
    placed: int
    bom_designators: int
    pages_registered: int
    pages_refused: int


@dataclass(frozen=True)
class Removed:
    reason: Any


@dataclass(frozen=True)
class NotUsed:
    reason: Any


@dataclass(frozen=True)
class Kept:
    reason: str
    after_bom_change: bool


PlacementsSummary = Union[Placed, Removed, NotUsed, Kept, WriteFailed]


def summary_text(summary: PlacementsSummary) -> str:
    """§8.5's wording for one member's outcome."""
    if isinstance(summary, Placed):
        text = f"Placements: {summary.placed:,} of {summary.bom_designators:,} parts placed"
        pages = summary.pages_registered + summary.pages_refused
        if pages:
            text += f"; {summary.pages_registered} of {pages} pages registered"
        return text
    if isinstance(summary, (Removed, NotUsed)):
        return f"placements unavailable: {reason_text(summary.reason)}"
    if isinstance(summary, Kept):
        if summary.after_bom_change:
            return f"placements not re-checked against the revised BOM: {summary.reason}"
        return f"placements unavailable: {summary.reason}"
    return f"placements not saved: {summary.cause}"


def superseded_extract_rows(plan: FamilyUpdatePlan, repositories: FamilyUpdateRepositories) -> list[SourceFile]:
    """The Placements rows a new extract replaces, read before any write, for the reaper after RELEASE."""
    rows: list[SourceFile] = []
    for audit_id, outcome in sorted(plan.placements.items()):
        if isinstance(outcome, PlacementsWritten) and outcome.new_extract is not None:
            row = repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PLACEMENTS)
            if row is not None:
                rows.append(row)
    return rows


def _recalculate(runtime_calc: Any, audit_id: int) -> None:
    if runtime_calc is not None:
        runtime_calc.persist(audit_id)


def _current_placements_row(audit_id: int, repositories: FamilyUpdateRepositories) -> SourceFile | None:
    return repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PLACEMENTS)


def _clear(row: SourceFile, repositories: FamilyUpdateRepositories) -> None:
    repositories.board_placements.delete_for_file(row.id)
    repositories.page_registrations.delete_for_placement_file(row.id)


def _write_member(
    audit_id: int,
    outcome: MemberPlacements,
    repositories: FamilyUpdateRepositories,
    runtime_calc: Any,
) -> PlacementsSummary:
    if isinstance(outcome, PlacementsRejected):
        repositories.placement_rejections.record(audit_id, outcome.rejection)
        return NotUsed(outcome.rejection.reason)

    if isinstance(outcome, PlacementsRemoved):
        row = _current_placements_row(audit_id, repositories)
        if row is not None:
            _clear(row, repositories)
        repositories.placement_rejections.record(audit_id, outcome.refusal)
        _recalculate(runtime_calc, audit_id)
        return Removed(outcome.refusal.reason)

    assert isinstance(outcome, PlacementsWritten)
    if outcome.new_extract is not None:
        extract = outcome.new_extract
        row = repositories.source_files.replace(audit_id, SourceFileCategory.PLACEMENTS, SourceFileDraft(
            audit_id=audit_id, file_category=SourceFileCategory.PLACEMENTS,
            original_filename=extract.original_filename, local_storage_path=extract.stored_path,
            file_hash=extract.file_hash, source_hash=extract.source_hash,
            source_size=extract.source_size, source_modified_ns=extract.source_modified_ns,
        ))
    else:
        current = _current_placements_row(audit_id, repositories)
        if current is None:
            raise LookupError("PLACEMENTS_ROW_MISSING")
        row = current
        _clear(row, repositories)

    repositories.board_placements.insert_many(row.id, outcome.admitted)
    if outcome.drawing is not None:
        drawing_id = outcome.drawing.drawing_source_file_id
        repositories.pdf_coords.delete_for_source_file(drawing_id)
        if outcome.drawing.labels:
            repositories.pdf_coords.bulk_insert([
                PdfComponentCoordDraft(
                    source_file_id=drawing_id, ref_des=c.ref_des, page_index=c.page_index,
                    x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2,
                )
                for c in outcome.drawing.labels
            ])
        repositories.page_registrations.replace(drawing_id, row.id, outcome.pages)
    _recalculate(runtime_calc, audit_id)
    registered = sum(1 for page in outcome.pages if isinstance(page, Registered))
    return Placed(len(outcome.admitted), outcome.bom_designators, registered, len(outcome.pages) - registered)


def apply_member_placements(
    audit_id: int,
    outcome: MemberPlacements,
    repositories: FamilyUpdateRepositories,
    runtime_calc: Any,
) -> PlacementsSummary:
    """Writes one member's placements in a savepoint of its own.

    pre:  UI thread; the family's savepoint has released, and nothing has run since
    post: per §8.5's table; runtimes recalculated after the member's placements; a failure rolls
          back this member alone and is WriteFailed; PlacementsKept writes nothing
    """
    if isinstance(outcome, PlacementsKept):
        return Kept(outcome.reason, outcome.after_bom_change)
    conn = repositories.conn
    conn.execute("SAVEPOINT update_placements")
    try:
        summary = _write_member(audit_id, outcome, repositories, runtime_calc)
        conn.execute("RELEASE SAVEPOINT update_placements")
        return summary
    except Exception as error:
        conn.execute("ROLLBACK TO SAVEPOINT update_placements")
        conn.execute("RELEASE SAVEPOINT update_placements")
        logger.warning("Placements not saved for audit %s: %s", audit_id, _describe(error))
        return WriteFailed(_describe(error))


def apply_family_placements(
    plan: FamilyUpdatePlan,
    repositories: FamilyUpdateRepositories,
    runtime_calc: Any,
) -> dict[int, PlacementsSummary]:
    """Every member's placements, one savepoint each, in audit order, with no event-loop turn between."""
    return {
        audit_id: apply_member_placements(audit_id, plan.placements[audit_id], repositories, runtime_calc)
        for audit_id in sorted(plan.placements)
    }
