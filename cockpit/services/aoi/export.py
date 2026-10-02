"""AOI XY export orchestration and the two-phase writer (Phase 52 sections 4, 6).

prepare_aoi_export does every network read and returns a plan or a question
for the operator; it touches no widget, so it can run off the UI thread.
write_aoi_files renders every file before touching disk, writes them all to
temporary files, and only then renames them onto their final names.
"""

import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Collection, Optional, Sequence, Union

from cockpit.persistence.repositories.source_files import SourceFileRepository

from .bom_designators import read_bom_designators
from .errors import (
    AoiDestinationExists, AoiDestinationRejected, AoiWriteError, BomUnavailable, FrameFitError,
    ProgramDecodeError, ProgramFileTooLarge, ProgramFormatUnsupported, ProgramRootUnreachable,
)
from .frame import derive_frame, marks_in_aoi_frame
from .juki_program import decode_program, locate_programs
from .mirtec import build_aoi_side, output_file_name, render_mirtec
from .types import (
    AoiFlag, AoiSideDocument, BoardSide, BomConflict, DecodedProgram, FlagKind, FrameTolerance,
    ProgramArticlePending, ProgramFile, ProgramFolderNotFound, ProgramLimits, ProgramsLocated,
    XyLimits, XyLocated, XyNotFound, XyPending,
)
from .xyrs import locate_xy, parse_xyrs


@dataclass(frozen=True)
class AoiExportRequest:
    job_number: str
    source_root: pathlib.Path
    stored_bom: pathlib.Path
    program_root: Optional[pathlib.Path] = None
    xy_path: Optional[pathlib.Path] = None
    article_chosen: bool = False
    article: Optional[str] = None
    xy_limits: XyLimits = field(default_factory=XyLimits)
    program_limits: ProgramLimits = field(default_factory=ProgramLimits)
    tolerance: FrameTolerance = field(default_factory=FrameTolerance)


@dataclass(frozen=True)
class AoiExportPlan:
    job_number: str
    xy_path: pathlib.Path
    program_folder: Optional[pathlib.Path]
    programs: tuple[ProgramFile, ...]
    top: Optional[AoiSideDocument]
    bottom: Optional[AoiSideDocument]
    notices: tuple[str, ...] = ()
    bom_conflicts: tuple[BomConflict, ...] = ()

    def documents(self) -> list[AoiSideDocument]:
        return [doc for doc in (self.top, self.bottom) if doc is not None]

    def document(self, side: BoardSide) -> Optional[AoiSideDocument]:
        return self.top if side is BoardSide.TOP else self.bottom

    def has_program(self, side: BoardSide) -> bool:
        return any(p.side is side for p in self.programs)

    def default_sides(self) -> tuple[BoardSide, ...]:
        """Sides worth exporting unless the operator says otherwise.

        post: when the job has programs, a side without one is left out: in the
              corpus that side holds hand-placed or mechanical rows, and the AOI
              programmer writes no file for it
        """
        return tuple(
            doc.side for doc in self.documents()
            if not self.programs or self.has_program(doc.side)
        )

    def default_bottom_start(self) -> int:
        return (self.top.fiducial_count if self.top is not None else 0) + 1


PrepareOutcome = Union[AoiExportPlan, XyPending, XyNotFound, ProgramArticlePending]


@dataclass(frozen=True)
class AoiWriteReport:
    written: tuple[pathlib.Path, ...]
    replaced: tuple[pathlib.Path, ...]


def resolve_stored_bom(audit_id: int, source_file_repo: SourceFileRepository) -> pathlib.Path:
    """The audit's stored Audit BOM.

    raises: BomUnavailable
    """
    bom_files = [f for f in source_file_repo.list_for_audit(audit_id) if f.file_category == "BOM"]
    if not bom_files:
        raise BomUnavailable("the audit has no BOM source file")
    path = pathlib.Path(bom_files[0].local_storage_path)
    if not path.exists():
        raise BomUnavailable(f"the stored BOM file is missing: {path}")
    return path


@dataclass(frozen=True)
class _ProgramSearch:
    pending: Optional[ProgramArticlePending] = None
    folder: Optional[pathlib.Path] = None
    programs: tuple[ProgramFile, ...] = ()


def _search_programs(request: AoiExportRequest, notices: list[str]) -> _ProgramSearch:
    """Finds the job's programs; a missing folder or root becomes a notice, not an error."""
    if request.program_root is None:
        notices.append(
            "The HLC program folder is not set (Settings › HLC Program Folder). Coordinates stay "
            "in the XY file's origin and fiducials come from the XY file, if it has any."
        )
        return _ProgramSearch()
    try:
        located = locate_programs(request.job_number, request.program_root)
    except ProgramRootUnreachable as exc:
        notices.append(f"{exc} Coordinates stay in the XY file's origin.")
        return _ProgramSearch()
    if isinstance(located, ProgramArticlePending):
        if not request.article_chosen:
            return _ProgramSearch(pending=located)
        located = located.choose(request.article)
    if isinstance(located, ProgramFolderNotFound):
        notices.append(f"No program folder at {located.searched}. Coordinates stay in the XY file's origin.")
        return _ProgramSearch()
    if not located.files:
        notices.append(f"No Juki programs in {located.folder}. Coordinates stay in the XY file's origin.")
    return _ProgramSearch(folder=located.folder, programs=located.files)


def _choose_marks(decoded: Sequence[DecodedProgram], flags: list[AoiFlag]):
    with_table = [d for d in decoded if d.marks is not None]
    if not with_table:
        flags.append(AoiFlag(
            FlagKind.MARKS_UNAVAILABLE,
            "This side's program format keeps its fiducials where Cockpit cannot read them yet",
        ))
        return None
    usable = [d for d in with_table if d.marks]
    if not usable:
        return None
    if any(d.marks != usable[0].marks for d in usable[1:]):
        flags.append(AoiFlag(
            FlagKind.MARKS_DISAGREE,
            f"Split programs list different fiducials; used {usable[0].file.path.name}",
        ))
    return usable[0].marks


def _build_side(side: BoardSide, xyrs, names, programs: Sequence[ProgramFile],
                request: AoiExportRequest, programs_expected: bool) -> Optional[AoiSideDocument]:
    cad = [p for p in xyrs.placements if p.side is side]
    cad_fiducials = [f for f in xyrs.fiducials if f.side is side]
    if not cad and not cad_fiducials:
        return None

    flags: list[AoiFlag] = []
    decoded: list[DecodedProgram] = []
    side_programs = [p for p in programs if p.side is side]
    designators = [p.ref_des for p in cad]
    for program in side_programs:
        try:
            decoded.append(decode_program(program, designators, request.program_limits))
        except (ProgramFileTooLarge, ProgramFormatUnsupported, ProgramDecodeError, OSError) as exc:
            flags.append(AoiFlag(FlagKind.PROGRAM_SKIPPED, str(exc)))

    frame = None
    marks = None
    if decoded:
        try:
            frame = derive_frame(side, cad, decoded, request.tolerance)
        except FrameFitError as exc:
            flags.append(AoiFlag(FlagKind.FRAME_UNAVAILABLE, f"{exc} Coordinates stay in the XY file's origin."))
    elif programs_expected:
        reason = "every program for this side was skipped" if side_programs else "no program for this side"
        flags.append(AoiFlag(FlagKind.FRAME_UNAVAILABLE, f"{reason}; coordinates stay in the XY file's origin"))

    if frame is not None:
        raw_marks = _choose_marks(decoded, flags)
        if raw_marks:
            marks = marks_in_aoi_frame(raw_marks, frame)

    return build_aoi_side(side, cad, cad_fiducials, names, frame, marks, flags)


def prepare_aoi_export(request: AoiExportRequest) -> "PrepareOutcome":
    """Gathers every input and builds both sides, TOP before BOTTOM.

    pre:  request.stored_bom is the audit's stored Audit BOM
    post: an AoiExportPlan, or the question the operator must answer first
          (XyPending, XyNotFound, ProgramArticlePending); no file is written
    raises: JobNumberMalformed, SourceRootUnreachable, JobDirectoryNotFound,
            JobDirectoryEscapesRoot, XyFileTooLarge, XyRowLimitExceeded,
            XyParseError, MalformedBomError, AoiBuildError, OSError
    """
    if request.xy_path is not None:
        xy_path = request.xy_path
    else:
        located = locate_xy(request.job_number, request.source_root)
        if not isinstance(located, XyLocated):
            return located
        xy_path = located.path

    xyrs = parse_xyrs(xy_path, request.xy_limits)
    cad_part_numbers = {p.ref_des: p.part_number for p in xyrs.placements}
    names = read_bom_designators(request.stored_bom, cad_part_numbers)

    notices: list[str] = []
    search = _search_programs(request, notices)
    if search.pending is not None:
        return search.pending
    programs_expected = bool(search.programs)

    top = _build_side(BoardSide.TOP, xyrs, names, search.programs, request, programs_expected)
    bottom = _build_side(BoardSide.BOTTOM, xyrs, names, search.programs, request, programs_expected)
    return AoiExportPlan(
        job_number=request.job_number.strip().upper(),
        xy_path=xy_path,
        program_folder=search.folder,
        programs=search.programs,
        top=top,
        bottom=bottom,
        notices=tuple(notices),
        bom_conflicts=names.conflicts,
    )


def _selected(plan: AoiExportPlan, sides: Optional[Collection[BoardSide]]) -> list[AoiSideDocument]:
    chosen = plan.default_sides() if sides is None else tuple(sides)
    return [doc for doc in plan.documents() if doc.side in chosen]


def planned_outputs(
    plan: AoiExportPlan,
    destination: pathlib.Path,
    sides: Optional[Collection[BoardSide]] = None,
) -> list[pathlib.Path]:
    return [destination / output_file_name(doc.side, plan.job_number) for doc in _selected(plan, sides)]


def _is_within(candidate: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def write_aoi_files(
    plan: AoiExportPlan,
    destination: pathlib.Path,
    bottom_start: Optional[int] = None,
    overwrite: bool = False,
    forbidden_roots: Sequence[pathlib.Path] = (),
    sides: Optional[Collection[BoardSide]] = None,
) -> AoiWriteReport:
    """Writes the plan's files: all to temporary files first, then renamed.

    pre:  the operator chose destination; bottom_start defaults to the top
          side's fiducial count + 1; sides defaults to plan.default_sides()
    post: on success every selected file exists with its final content; on any
          failure no temporary file remains, and the error names every final
          file that was already written or replaced
    raises: AoiDestinationRejected, AoiDestinationExists, AoiWriteError
    """
    if not destination.is_dir():
        raise AoiDestinationRejected(destination, "not an existing folder")
    for root in forbidden_roots:
        if _is_within(destination, root):
            raise AoiDestinationRejected(destination, "it is inside Cockpit's managed storage")
    documents = _selected(plan, sides)
    if not documents:
        raise AoiDestinationRejected(destination, "no side is selected for export")

    start = bottom_start if bottom_start is not None else plan.default_bottom_start()
    targets = [
        (destination / output_file_name(doc.side, plan.job_number),
         render_mirtec(doc, 1 if doc.side is BoardSide.TOP else start))
        for doc in documents
    ]
    existing = tuple(path for path, _text in targets if path.exists())
    if existing and not overwrite:
        raise AoiDestinationExists(existing)

    temps: list[pathlib.Path] = []
    written: list[pathlib.Path] = []
    replaced: list[pathlib.Path] = []
    try:
        for final, text in targets:
            handle, temp_name = tempfile.mkstemp(prefix=f".{final.name}.", suffix=".tmp", dir=destination)
            temps.append(pathlib.Path(temp_name))
            with os.fdopen(handle, "wb") as stream:
                stream.write(text.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
        for (final, _text), temp in zip(targets, temps):
            existed = final.exists()
            os.replace(temp, final)
            written.append(final)
            if existed:
                replaced.append(final)
    except OSError as exc:
        raise AoiWriteError(destination, exc, tuple(written), tuple(replaced)) from exc
    finally:
        for temp in temps:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return AoiWriteReport(tuple(written), tuple(replaced))
