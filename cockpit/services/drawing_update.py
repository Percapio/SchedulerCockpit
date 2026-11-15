"""*Update ▸ Drawing* off the UI thread (Architecture/20261115-Phase57.md §9.2).

add_pdf_to_audit hashes, copies, parses, writes and reaps in one call on the UI thread. Fitting a
drawing's pages can take 27.5 s, so the call splits at the database boundary:
- read_drawing_update_inputs (UI thread) reads what the worker needs, as values;
- prepare_drawing_update (drawing worker) copies, parses and fits, and writes no rows;
- apply_drawing_update (UI thread) checks the rows the inputs named, then writes in one savepoint.
The secondary drawing keeps add_secondary_pdf_to_audit: it has no labels.
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
from dataclasses import dataclass
from typing import Any, Union

from cockpit.ingestion import hashing
from cockpit.ingestion.errors import HashingError, ParseError
from cockpit.ingestion.roles import SourceRole
from cockpit.ingestion.service import content_addressed_path
from cockpit.layout.results import PdfComponentCoordinate
from cockpit.persistence.types import PdfComponentCoordDraft, SourceFileCategory, SourceFileDraft
from cockpit.placements.bmb_file import BmbPlacement, direct_share_call
from cockpit.placements.registration import (
    DrawingLabel,
    PageOutcome,
    Registered,
    RegistrationStopped,
    register_drawing_pages,
)
from cockpit.services.document_updates import (
    PARTIAL_COPY_SUFFIX,
    UNSPLIT_DIRECTORY,
    Cancelled,
    FamilyUpdateRepositories,
    StagedDocument,
    StorageFailed,
    WriteFailed,
    _describe,
    discard_copies,
)
from cockpit.services.storage_reaper import StorageReaper

logger = logging.getLogger(__name__)

RowIdentity = tuple[int, str]


@dataclass(frozen=True)
class DrawingUpdateInputs:
    """The audit's rows the drawing is read against, each as (id, file_hash), read on the UI thread."""

    audit_id: int
    part_number: str
    bom_row: RowIdentity | None
    bom_path: pathlib.Path | None
    designators: frozenset[str]
    placement_row: RowIdentity | None
    admitted: tuple[BmbPlacement, ...]


@dataclass(frozen=True)
class DrawingUpdatePlan:
    inputs: DrawingUpdateInputs
    staged_pdf: StagedDocument
    original_filename: str
    labels: tuple[PdfComponentCoordinate, ...]
    pages: tuple[PageOutcome, ...]


@dataclass(frozen=True)
class DrawingUpdated:
    audit_id: int
    labels_found: int
    pages_registered: int
    pages_refused: int


@dataclass(frozen=True)
class AuditChangedMeanwhile:
    audit_id: int


@dataclass(frozen=True)
class DrawingUnreadable:
    """reason is the parser's reason code (RASTER_PDF, UNREADABLE_PDF) or the error's kind."""

    reason: str


DrawingUpdateFailure = Union[StorageFailed, DrawingUnreadable, Cancelled]


@dataclass(frozen=True)
class DrawingUpdateFailed:
    failure: DrawingUpdateFailure
    copied_by_this_run: StagedDocument | None


def _identity(row: Any) -> RowIdentity | None:
    return (row.id, row.file_hash) if row is not None else None


def read_drawing_update_inputs(audit_id: int, repositories: FamilyUpdateRepositories) -> DrawingUpdateInputs | None:
    """Reads, on the UI thread, everything prepare_drawing_update needs; None when the audit is gone."""
    audit = repositories.audits.find_by_id(audit_id)
    if audit is None:
        return None
    bom = repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
    designators = (
        frozenset(c.ref_des for c in repositories.bom_components.list_for_source_file(bom.id))
        if bom is not None else frozenset()
    )
    placements = repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PLACEMENTS)
    admitted = tuple(repositories.board_placements.list_for_file(placements.id)) if placements is not None else ()
    return DrawingUpdateInputs(
        audit_id=audit_id,
        part_number=audit.part_number,
        bom_row=_identity(bom),
        bom_path=bom.local_storage_path if bom is not None else None,
        designators=designators,
        placement_row=_identity(placements),
        admitted=admitted,
    )


def _remove_quietly(path: pathlib.Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        logger.warning("A partial drawing copy could not be removed; the startup sweep will: %s", _describe(error))


def _dni_designators(bom_path: pathlib.Path | None, bom_parser: Any) -> frozenset[str]:
    if bom_path is None or bom_parser is None:
        return frozenset()
    try:
        return frozenset(getattr(bom_parser.parse(bom_path), "dni_designators", ()))
    except Exception as error:
        logger.warning("The stored BOM couldn't be parsed for its DNI designators; splitting without them: %s",
                       _describe(error))
        return frozenset()


def _stage_pdf(
    pdf_path: pathlib.Path,
    destination_dir: pathlib.Path,
    share_call: Any,
    stopped: Any,
    run_id: str,
) -> StagedDocument | DrawingUpdateFailed:
    """One share read: the copy goes to a partial file, which is hashed locally and moved into place."""
    partial = destination_dir / f"{pdf_path.stem}.{run_id}{pdf_path.suffix}{PARTIAL_COPY_SUFFIX}"
    try:
        share_call(lambda: shutil.copy2(pdf_path, partial))
    except OSError as error:
        _remove_quietly(partial)
        return DrawingUpdateFailed(StorageFailed(_describe(error)), None)
    if stopped():
        _remove_quietly(partial)
        return DrawingUpdateFailed(Cancelled(), None)
    try:
        pdf_hash = hashing.sha256_hex(partial)
    except (HashingError, OSError) as error:
        _remove_quietly(partial)
        return DrawingUpdateFailed(StorageFailed(_describe(error)), None)

    destination = content_addressed_path(destination_dir, pdf_path, pdf_hash)
    if destination.exists():
        _remove_quietly(partial)
        try:
            existing = hashing.sha256_hex(destination)
        except (HashingError, OSError) as error:
            return DrawingUpdateFailed(StorageFailed(_describe(error)), None)
        if existing != pdf_hash:
            return DrawingUpdateFailed(StorageFailed("STORED_COPY_MISMATCH"), None)
        return StagedDocument(SourceRole.PDF, pdf_path.name, destination, pdf_hash, False)
    if stopped():
        _remove_quietly(partial)
        return DrawingUpdateFailed(Cancelled(), None)
    try:
        os.replace(partial, destination)
    except OSError as error:
        _remove_quietly(partial)
        return DrawingUpdateFailed(StorageFailed(_describe(error)), None)
    return StagedDocument(SourceRole.PDF, pdf_path.name, destination, pdf_hash, True)


def prepare_drawing_update(
    inputs: DrawingUpdateInputs,
    pdf_path: pathlib.Path,
    storage_root: pathlib.Path,
    layout_parser: Any,
    bom_parser: Any,
    share_call: Any = direct_share_call,
    stop: Any = None,
    run_id: str = "drawing",
) -> DrawingUpdatePlan | DrawingUpdateFailed:
    """Copies, parses and fits a new drawing; writes no rows.

    pre:  runs on the drawing worker; inputs were read on the UI thread
    post: the PDF is at its content-addressed path and hashes to its own hash, copied through a
          partial file in one share call; the stored BOM is parsed for its DNI designators (none
          when it doesn't parse); labels are parsed with keep_targets the designators and
          split_targets those plus the DNI designators; pages are registered when admitted isn't
          empty; a PDF the parser refuses, RASTER_PDF included, is DrawingUnreadable; stop is
          checked after the copy, before the move into place, before fitting and before returning;
          on any failure after the move, copied_by_this_run names the copy for the UI thread to
          discard
    """
    def stopped() -> bool:
        return stop is not None and stop.is_set()

    destination_dir = storage_root / inputs.part_number / UNSPLIT_DIRECTORY
    try:
        destination_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return DrawingUpdateFailed(StorageFailed(_describe(error)), None)
    if stopped():
        return DrawingUpdateFailed(Cancelled(), None)

    staged = _stage_pdf(pathlib.Path(pdf_path), destination_dir, share_call, stopped, run_id)
    if isinstance(staged, DrawingUpdateFailed):
        return staged
    moved = staged if staged.copied_by_this_run else None

    dni = _dni_designators(inputs.bom_path, bom_parser)
    try:
        layout = layout_parser.parse(
            staged.stored_path, set(inputs.designators), split_targets=set(inputs.designators) | set(dni),
        )
    except ParseError as error:
        return DrawingUpdateFailed(DrawingUnreadable(error.reason), moved)
    except Exception as error:
        return DrawingUpdateFailed(DrawingUnreadable(_describe(error)), moved)
    labels = tuple(layout.coordinates)

    pages: tuple[PageOutcome, ...] = ()
    if inputs.admitted:
        if stopped():
            return DrawingUpdateFailed(Cancelled(), moved)
        outcome = register_drawing_pages(
            {placement.designator: placement for placement in inputs.admitted},
            [DrawingLabel(c.ref_des, c.page_index, (c.x1, c.y1, c.x2, c.y2), c.from_split) for c in labels],
            stop,
        )
        if isinstance(outcome, RegistrationStopped):
            return DrawingUpdateFailed(Cancelled(), moved)
        pages = tuple(outcome)
    if stopped():
        return DrawingUpdateFailed(Cancelled(), moved)
    return DrawingUpdatePlan(inputs, staged, pathlib.Path(pdf_path).name, labels, pages)


def _audit_changed(inputs: DrawingUpdateInputs, repositories: FamilyUpdateRepositories) -> bool:
    if repositories.audits.find_by_id(inputs.audit_id) is None:
        return True
    for category, expected in (
        (SourceFileCategory.BOM, inputs.bom_row),
        (SourceFileCategory.PLACEMENTS, inputs.placement_row),
    ):
        if _identity(repositories.source_files.find_by_audit_and_category(inputs.audit_id, category)) != expected:
            return True
    return False


def discard_drawing_copy(failed: DrawingUpdateFailed, repositories: FamilyUpdateRepositories) -> None:
    """Unlinks a failed run's moved copy when no row references it, as discard_copies does."""
    if failed.copied_by_this_run is not None:
        discard_copies((failed.copied_by_this_run,), repositories.source_files)


def apply_drawing_update(
    plan: DrawingUpdatePlan,
    repositories: FamilyUpdateRepositories,
    runtime_calc: Any,
) -> DrawingUpdated | AuditChangedMeanwhile | WriteFailed:
    """Writes a prepared drawing.

    pre:  UI thread
    post: iff the audit still exists and its BOM and Placements rows are still the ones inputs named
          (by id and hash), one savepoint replaces the drawing row, inserts its labels, writes its
          registrations and recalculates runtimes, and the superseded drawing is reaped after
          RELEASE; otherwise nothing is written, the copy is discarded when no row references it,
          and AuditChangedMeanwhile is returned
    """
    inputs = plan.inputs
    copies = (plan.staged_pdf,) if plan.staged_pdf.copied_by_this_run else ()
    if _audit_changed(inputs, repositories):
        discard_copies(copies, repositories.source_files)
        return AuditChangedMeanwhile(inputs.audit_id)

    prior = repositories.source_files.find_by_audit_and_category(inputs.audit_id, SourceFileCategory.PDF)
    conn = repositories.conn
    conn.execute("SAVEPOINT update_drawing")
    try:
        drawing = repositories.source_files.replace(inputs.audit_id, SourceFileCategory.PDF, SourceFileDraft(
            audit_id=inputs.audit_id, file_category=SourceFileCategory.PDF,
            original_filename=plan.original_filename, local_storage_path=plan.staged_pdf.stored_path,
            file_hash=plan.staged_pdf.file_hash,
        ))
        if plan.labels:
            repositories.pdf_coords.bulk_insert([
                PdfComponentCoordDraft(
                    source_file_id=drawing.id, ref_des=c.ref_des, page_index=c.page_index,
                    x1=c.x1, y1=c.y1, x2=c.x2, y2=c.y2,
                )
                for c in plan.labels
            ])
        if inputs.placement_row is not None:
            repositories.page_registrations.replace(drawing.id, inputs.placement_row[0], plan.pages)
        if runtime_calc is not None:
            runtime_calc.persist(inputs.audit_id)
        conn.execute("RELEASE SAVEPOINT update_drawing")
    except Exception as error:
        conn.execute("ROLLBACK TO SAVEPOINT update_drawing")
        conn.execute("RELEASE SAVEPOINT update_drawing")
        logger.warning("Drawing update rolled back for audit %s: %s", inputs.audit_id, _describe(error))
        discard_copies(copies, repositories.source_files)
        return WriteFailed(_describe(error))

    if prior is not None:
        try:
            report = StorageReaper(repositories.source_files).reap([prior])
            if report.failed_paths:
                logger.warning("The superseded drawing could not be removed; the startup sweep will retry")
        except Exception as error:
            logger.warning("Reaping the superseded drawing failed: %s", _describe(error))

    registered = sum(1 for page in plan.pages if isinstance(page, Registered))
    return DrawingUpdated(inputs.audit_id, len(plan.labels), registered, len(plan.pages) - registered)


RASTER_PDF = "RASTER_PDF"


def failure_text(failure: Any) -> str:
    """What the operator reads when *Update ▸ Drawing* doesn't land."""
    if isinstance(failure, AuditChangedMeanwhile):
        return "The audit changed while the drawing was read; add it again."
    if isinstance(failure, DrawingUnreadable):
        if failure.reason == RASTER_PDF:
            return "This PDF has no text layer, so Cockpit can't find designators on it."
        return f"The drawing couldn't be read ({failure.reason})."
    if isinstance(failure, StorageFailed):
        return f"The drawing couldn't be stored ({failure.cause})."
    if isinstance(failure, WriteFailed):
        return f"The drawing couldn't be saved ({failure.cause})."
    if isinstance(failure, Cancelled):
        return "Cancelled."
    return type(failure).__name__


def success_text(updated: DrawingUpdated) -> str:
    text = f"Drawing updated: {updated.labels_found:,} labels found"
    pages = updated.pages_registered + updated.pages_refused
    if pages:
        text += f"; {updated.pages_registered} of {pages} pages registered"
    return text
