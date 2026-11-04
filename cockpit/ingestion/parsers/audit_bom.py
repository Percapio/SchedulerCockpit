"""Audit BOM parser."""

from dataclasses import dataclass, field
from enum import StrEnum
import logging
import pathlib
import re
import time
from typing import Any, Final, Sequence
import zipfile

import openpyxl
from openpyxl.cell.rich_text import CellRichText, TextBlock

if CellRichText is None or TextBlock is None:
    raise ImportError("openpyxl >= 3.1 with CellRichText support is required")

from ..errors import MalformedBomError
from ..filename_rules import derive_part_number_from_filename
from ..labels import header_key
from ..locator import JOB_NUMBER_GRAMMAR
from .results import BomItem, BomResult, HeaderLayout

logger = logging.getLogger(__name__)


class ColumnName(StrEnum):
    FIND_NUMBER = "Find#"
    PART_NUMBER = "PartNum"
    COUNT = "Count"
    MSL_LEVEL = "MSL level"
    DATE_CODE = "Date code"
    BAKED_DATE = "Baked date"
    REF_DES = "Ref_Des"
    PACKAGE = "Package"
    DESCRIPTION = "Description"
    MOUNT_TYPE = "SMT/THT"
    QTY_NEED = "Qty Need"
    QTY_ON_HAND = "Qty On hand"
    QTY_SHORT = "Qty short"
    COMMENT = "comment"


class MountType(StrEnum):
    SMT = "SMT"
    THT = "THT"


class RefDesKind(StrEnum):
    DESIGNATORS = "DESIGNATORS"
    EMPTY = "EMPTY"
    PLACEHOLDER = "PLACEHOLDER"
    FREE_TEXT = "FREE_TEXT"


RefDes = str

ANCHOR_COLUMNS: Final[list[ColumnName]] = [
    ColumnName.FIND_NUMBER,
    ColumnName.PART_NUMBER,
]
CONSUMED_COLUMNS: Final[list[ColumnName]] = [
    ColumnName.FIND_NUMBER,
    ColumnName.PART_NUMBER,
    ColumnName.REF_DES,
    ColumnName.DESCRIPTION,
    ColumnName.MOUNT_TYPE,
]
DISPLAY_COLUMNS: Final[list[ColumnName]] = [
    ColumnName.COUNT,
    ColumnName.PACKAGE,
    ColumnName.QTY_NEED,
    ColumnName.QTY_ON_HAND,
    ColumnName.QTY_SHORT,
    ColumnName.COMMENT,
    ColumnName.MSL_LEVEL,
    ColumnName.DATE_CODE,
    ColumnName.BAKED_DATE,
]
HEADER_SEARCH_ROWS: Final[int] = 5
CLEANING_RULES_VERSION: Final[int] = 1

REFDES_TOKEN_REGEX: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.+$/-]+$")
DESIGNATOR_SHAPE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z]+\S*[0-9]")
NOTE_SEPARATOR: Final[re.Pattern[str]] = re.compile(r"[\r\n]+|[ \t]{2,}")
DESIGNATOR_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"n/a", "na", "tbd", "?", "-", "none"}
)
QUOTE_PAIRS: Final[dict[str, str]] = {
    '"': '"',
    "\u201c": "\u201d",
    "'": "'",
    "\u2018": "\u2019",
}
SILENT_MOUNT_TEXTS: Final[frozenset[str]] = frozenset({"PCB", "DNI"})

_PAIRED_NOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"\*([^*]*)\*"
    r"|\(([^)]*)\)"
    r"|\{([^}]*)\}"
    r"|\[([^\]]*)\]"
)

REQUIRED_HEADER: Final[list[str]] = [
    "Find#", "PartNum", "Count",
    "Ref_Des", "Package", "Description", "SMT/THT",
    "Qty Need", "Qty On hand", "Qty short", "comment",
]

OPTIONAL_HEADER: Final[frozenset[str]] = frozenset(
    {"MSL level", "Date code", "Baked date"}
)

CANONICAL_COLUMNS: tuple[str, ...] = (
    "Find#", "PartNum", "Count", "Ref_Des", "Package", "Description",
    "SMT/THT", "Qty Need", "Qty On hand", "Qty short", "comment",
    "MSL level", "Date code", "Baked date",
)

# The Audit BOM sheet's physical column order: the same fourteen labels as
# CANONICAL_COLUMNS, positioned as they appear in the workbook rather than in
# canonical emission order. The three optional labels sit at 3-5; see
# Patch 09b section 2.2 for the derivation.
SHEET_COLUMN_ORDER: tuple[str, ...] = (
    "Find#", "PartNum", "Count", "MSL level", "Date code", "Baked date",
    "Ref_Des", "Package", "Description", "SMT/THT", "Qty Need", "Qty On hand",
    "Qty short", "comment",
)


def sheet_column_order_is_consistent() -> bool:
    """Holds when SHEET_COLUMN_ORDER describes a layout matching REQUIRED_HEADER and CANONICAL_COLUMNS."""
    without_optionals = [label for label in SHEET_COLUMN_ORDER if label not in OPTIONAL_HEADER]
    if without_optionals != REQUIRED_HEADER:
        return False

    if set(SHEET_COLUMN_ORDER) != set(CANONICAL_COLUMNS):
        return False

    if len(set(SHEET_COLUMN_ORDER)) != len(SHEET_COLUMN_ORDER):
        return False

    return True


if not sheet_column_order_is_consistent():
    raise RuntimeError(
        "SHEET_COLUMN_ORDER invariant broken: It does not match REQUIRED_HEADER/CANONICAL_COLUMNS expectations."
    )


_CANONICAL_COLUMN_ENUM_ORDER: Final[tuple[ColumnName, ...]] = tuple(
    ColumnName(label) for label in CANONICAL_COLUMNS
)
_CONSUMED_BY_KEY: Final[dict[str, ColumnName]] = {
    header_key(col.value): col for col in CONSUMED_COLUMNS
}
_DISPLAY_BY_KEY: Final[dict[str, ColumnName]] = {
    header_key(col.value): col for col in DISPLAY_COLUMNS
}
_ANCHOR_KEYS: Final[frozenset[str]] = frozenset(
    header_key(col.value) for col in ANCHOR_COLUMNS
)
_CONSUMED_KEYS: Final[frozenset[str]] = frozenset(
    header_key(col.value) for col in CONSUMED_COLUMNS
)


# ---- Records ---------------------------------------------------------------

@dataclass(frozen=True)
class TextRun:
    text: str
    is_struck: bool = False
    has_own_font: bool = field(default=False, compare=False, repr=False)


@dataclass(frozen=True)
class SheetCell:
    value: Any = None
    runs: list[TextRun] = field(default_factory=list)
    is_cell_struck: bool = False


@dataclass(frozen=True)
class CellReading:
    raw_text: str
    visible_text: str
    struck_text: str


@dataclass(frozen=True)
class SheetHeaderProbe:
    sheet_name: str
    is_visible: bool
    header_row: int | None
    is_complete: bool
    observed_labels: list[str]


@dataclass(frozen=True)
class ChosenSheet:
    sheet_name: str
    header_row: int
    is_visible: bool
    observed_labels: list[str]


@dataclass(frozen=True)
class NoSheetHasHeader:
    probes: list[SheetHeaderProbe]


@dataclass(frozen=True)
class SheetsAmbiguous:
    candidates: list[str]


SheetSelectionFailure = NoSheetHasHeader | SheetsAmbiguous


@dataclass(frozen=True)
class BomColumnMap:
    consumed: dict[ColumnName, int]
    display: dict[ColumnName, int]
    ignored_labels: list[str]


@dataclass(frozen=True)
class ConsumedColumnMissing:
    missing: list[ColumnName]


@dataclass(frozen=True)
class ConsumedColumnDuplicated:
    duplicated: list[ColumnName]


HeaderRejected = ConsumedColumnMissing | ConsumedColumnDuplicated


@dataclass(frozen=True)
class BomNote:
    sheet_row: int
    find_number_text: str
    column: ColumnName
    text: str


@dataclass(frozen=True)
class StruckTextRemoved:
    sheet_row: int
    column: ColumnName


@dataclass(frozen=True)
class EnclosingQuotesRemoved:
    sheet_row: int
    column: ColumnName


@dataclass(frozen=True)
class NoteExtracted:
    note: BomNote

    @property
    def sheet_row(self) -> int:
        return self.note.sheet_row


@dataclass(frozen=True)
class LineRemovedByStrike:
    sheet_row: int
    struck_part_number: str


@dataclass(frozen=True)
class PartNumberReplacedByStrike:
    sheet_row: int
    struck: str
    kept: str


@dataclass(frozen=True)
class MountTypeReplacedByStrike:
    sheet_row: int
    struck: str
    kept: MountType


@dataclass(frozen=True)
class DesignatorsStruck:
    sheet_row: int
    struck: list[RefDes]


@dataclass(frozen=True)
class DesignatorPlaceholder:
    sheet_row: int
    text: str


@dataclass(frozen=True)
class FreeTextInRefDes:
    sheet_row: int
    text: str


@dataclass(frozen=True)
class DesignatorRepeatDropped:
    sheet_row: int
    designator: RefDes


@dataclass(frozen=True)
class UnknownMountTypeSkipped:
    sheet_row: int
    part_number: str
    mount_text: str


@dataclass(frozen=True)
class HiddenSheetRead:
    sheet_name: str


CleaningAction = (
    StruckTextRemoved
    | EnclosingQuotesRemoved
    | NoteExtracted
    | LineRemovedByStrike
    | PartNumberReplacedByStrike
    | MountTypeReplacedByStrike
    | DesignatorsStruck
    | DesignatorPlaceholder
    | FreeTextInRefDes
    | DesignatorRepeatDropped
    | UnknownMountTypeSkipped
    | HiddenSheetRead
)

CleaningActionKind = type[CleaningAction]

CONSEQUENTIAL_ACTIONS: Final[frozenset[type]] = frozenset({
    LineRemovedByStrike,
    PartNumberReplacedByStrike,
    MountTypeReplacedByStrike,
    DesignatorsStruck,
    DesignatorPlaceholder,
    FreeTextInRefDes,
    UnknownMountTypeSkipped,
    HiddenSheetRead,
})

ROUTINE_ORDER: Final[list[type]] = [
    NoteExtracted,
    EnclosingQuotesRemoved,
    StruckTextRemoved,
    DesignatorRepeatDropped,
]

ROUTINE_PHRASES: Final[dict[type, tuple[str, str]]] = {
    NoteExtracted: ("note", "notes"),
    EnclosingQuotesRemoved: ("quoted value", "quoted values"),
    StruckTextRemoved: ("struck-through cell", "struck-through cells"),
    DesignatorRepeatDropped: ("repeated designator", "repeated designators"),
}


@dataclass(frozen=True)
class QuoteUnwrap:
    value: str
    was_unwrapped: bool


@dataclass(frozen=True)
class NotesExtraction:
    remainder: str
    notes: list[str]


@dataclass(frozen=True)
class SeparatorCut:
    head: str
    note: str | None


@dataclass(frozen=True)
class PartNumberSplit:
    part_number: str
    note: str | None


@dataclass(frozen=True)
class RefDesSplit:
    designators: list[RefDes]
    repeats: list[RefDes]
    notes: list[str]
    kind: RefDesKind
    residual: str | None = None


@dataclass(frozen=True)
class TokenRejected:
    token: str


@dataclass(frozen=True)
class CleanedRow:
    sheet_row: int
    cells: dict[ColumnName, str]
    designators: list[RefDes]
    notes: list[BomNote]
    is_removed_line: bool
    actions: list[CleaningAction]


@dataclass(frozen=True)
class DesignatorRejected:
    sheet_row: int
    cell_text: str
    token: str
    find_number_text: str = field(default="", compare=False, repr=False)
    cells: dict[ColumnName, str] = field(default_factory=dict, compare=False, repr=False)
    notes: list[BomNote] = field(default_factory=list, compare=False, repr=False)


RowRejected = DesignatorRejected


@dataclass(frozen=True)
class CleanedSheet:
    sheet: ChosenSheet
    column_map: BomColumnMap
    header_layout: HeaderLayout
    rows: list[CleanedRow | DesignatorRejected]


@dataclass(frozen=True)
class CleaningReport:
    rules_version: int
    sheet_name: str
    sheet_is_visible: bool
    header_row: int
    actions: list[CleaningAction]
    rejected_rows: list[DesignatorRejected]


@dataclass(frozen=True)
class CleaningSummary:
    routine_counts: dict[type, int]
    consequential: list[CleaningAction]


EMPTY_CLEANING_SUMMARY: Final[CleaningSummary] = CleaningSummary(
    routine_counts={}, consequential=[]
)


@dataclass(frozen=True)
class RawBomRow:
    sheet_ordinal: int          # 1-based physical row index; sheet order
    cells: tuple[str, ...]      # exactly 14, CANONICAL_COLUMNS order, "" absent
    part_number: str            # PartNum, cleaned
    description: str            # Description, cleaned
    designators: list[RefDes] = field(default_factory=list)
    notes: list[BomNote] = field(default_factory=list)


# ---- Pure helpers & cell reading -------------------------------------------

def normalize_sheet_label(label: str) -> str:
    """Normalize sheet label for case-insensitive matching."""
    return str(label).strip().casefold()


def _cell_font_strike(cell: Any) -> bool:
    """Whole-cell strikethrough, tolerant of read-only/empty cells."""
    try:
        font = cell.font
    except AttributeError:
        return False
    return bool(font is not None and getattr(font, "strike", False))


def sheet_cell_of(cell: Any) -> SheetCell:
    """Convert an openpyxl cell (or return an existing SheetCell) to a SheetCell."""
    if isinstance(cell, SheetCell):
        return cell
    value = getattr(cell, "value", None)
    cell_struck = _cell_font_strike(cell)
    if value is None:
        return SheetCell(value=None, runs=[], is_cell_struck=cell_struck)
    if isinstance(value, CellRichText):
        runs: list[TextRun] = []
        for item in value:
            if isinstance(item, TextBlock):
                font = item.font
                run_struck = bool(font is not None and getattr(font, "strike", None))
                runs.append(TextRun(text=item.text or "", is_struck=run_struck, has_own_font=True))
            else:
                runs.append(TextRun(text=str(item), is_struck=cell_struck, has_own_font=False))
        return SheetCell(value=str(value), runs=runs, is_cell_struck=cell_struck)
    return SheetCell(value=value, runs=[], is_cell_struck=cell_struck)


def read_cell(cell: SheetCell | Any) -> CellReading:
    """Splits a cell into the text a reader sees and the text that is struck through."""
    sc = sheet_cell_of(cell)
    if sc.runs:
        raw_parts: list[str] = []
        vis_parts: list[str] = []
        struck_parts: list[str] = []
        for run in sc.runs:
            text = run.text or ""
            raw_parts.append(text)
            is_struck = bool(
                run.is_struck
                or (not run.has_own_font and sc.is_cell_struck)
            )
            if is_struck:
                struck_parts.append(text)
            else:
                vis_parts.append(text)
        return CellReading(
            raw_text="".join(raw_parts),
            visible_text="".join(vis_parts),
            struck_text="".join(struck_parts),
        )

    if sc.value is None:
        return CellReading(raw_text="", visible_text="", struck_text="")

    text = str(sc.value)
    if sc.is_cell_struck:
        return CellReading(raw_text=text, visible_text="", struck_text=text)
    return CellReading(raw_text=text, visible_text=text, struck_text="")


def read_columns(
    cells: Sequence[SheetCell | Any],
    column_map: BomColumnMap,
) -> dict[ColumnName, CellReading]:
    """Reads the consumed and display cells a column map names."""
    all_mapped: list[tuple[ColumnName, int]] = list(column_map.consumed.items()) + list(
        column_map.display.items()
    )
    all_mapped.sort(key=lambda pair: pair[1])
    readings: dict[ColumnName, CellReading] = {}
    for col, idx in all_mapped:
        if 0 <= idx < len(cells):
            readings[col] = read_cell(cells[idx])
        else:
            readings[col] = CellReading(raw_text="", visible_text="", struck_text="")
    return readings


# ---- Sheet probing and header resolution (§3.2, §3.3) ----------------------

def _trim_trailing_blanks(labels: list[str]) -> list[str]:
    trimmed = list(labels)
    while trimmed and trimmed[-1] == "":
        trimmed.pop()
    return trimmed


def probe_sheets(workbook: openpyxl.Workbook) -> list[SheetHeaderProbe]:
    """Records where each sheet's header is, reading only its first rows."""
    probes: list[SheetHeaderProbe] = []
    for sheet_name in workbook.sheetnames:
        ws = workbook[sheet_name]
        is_visible = getattr(ws, "sheet_state", "visible") == "visible"
        header_row_num: int | None = None
        is_complete = False
        chosen_labels: list[str] = []
        first_non_empty_labels: list[str] = []

        for row_idx, row_cells in enumerate(
            ws.iter_rows(min_row=1, max_row=HEADER_SEARCH_ROWS), start=1
        ):
            labels = [read_cell(c).visible_text.strip() for c in row_cells]
            trimmed = _trim_trailing_blanks(labels)
            if trimmed and not first_non_empty_labels:
                first_non_empty_labels = trimmed

            row_keys = {header_key(lbl) for lbl in trimmed if lbl}
            if _ANCHOR_KEYS.issubset(row_keys):
                header_row_num = row_idx
                is_complete = _CONSUMED_KEYS.issubset(row_keys)
                chosen_labels = trimmed
                break

        probes.append(
            SheetHeaderProbe(
                sheet_name=sheet_name,
                is_visible=is_visible,
                header_row=header_row_num,
                is_complete=is_complete,
                observed_labels=chosen_labels if header_row_num is not None else first_non_empty_labels,
            )
        )
    return probes


def choose_bom_sheet(
    probes: list[SheetHeaderProbe],
    declared_job_number: str,
) -> ChosenSheet | SheetSelectionFailure:
    """Chooses the worksheet holding the Audit BOM by its header, not its name."""
    headed = [p for p in probes if p.header_row is not None]
    if not headed:
        return NoSheetHasHeader(probes=list(probes))

    tiers: list[list[SheetHeaderProbe]] = [
        [p for p in headed if p.is_complete and p.is_visible],
        [p for p in headed if p.is_complete and not p.is_visible],
        [p for p in headed if not p.is_complete and p.is_visible],
        [p for p in headed if not p.is_complete and not p.is_visible],
    ]
    preferred = next(tier for tier in tiers if tier)
    if len(preferred) == 1:
        p = preferred[0]
        assert p.header_row is not None
        return ChosenSheet(
            sheet_name=p.sheet_name,
            header_row=p.header_row,
            is_visible=p.is_visible,
            observed_labels=list(p.observed_labels),
        )

    by_name: list[list[SheetHeaderProbe]] = [
        [
            p for p in preferred
            if normalize_sheet_label(p.sheet_name) == normalize_sheet_label("AUDIT BOM")
        ]
    ]
    if JOB_NUMBER_GRAMMAR.match(declared_job_number):
        by_name.append([
            p for p in preferred
            if normalize_sheet_label(p.sheet_name) == normalize_sheet_label(declared_job_number)
        ])
        by_name.append([
            p for p in preferred
            if declared_job_number.casefold() in p.sheet_name.casefold()
        ])

    for survivors in by_name:
        if len(survivors) == 1:
            s = survivors[0]
            assert s.header_row is not None
            return ChosenSheet(
                sheet_name=s.sheet_name,
                header_row=s.header_row,
                is_visible=s.is_visible,
                observed_labels=list(s.observed_labels),
            )

    return SheetsAmbiguous(candidates=[p.sheet_name for p in preferred])


def resolve_bom_header(
    observed_labels: list[str],
) -> BomColumnMap | HeaderRejected:
    """Maps the chosen header row to column positions by normalised label."""
    consumed: dict[ColumnName, int] = {}
    consumed_counts: dict[ColumnName, int] = {col: 0 for col in CONSUMED_COLUMNS}
    display: dict[ColumnName, int] = {}
    ignored_labels: list[str] = []

    for idx, raw_label in enumerate(observed_labels):
        label = raw_label.strip() if raw_label else ""
        if not label:
            continue
        key = header_key(label)
        if key in _CONSUMED_BY_KEY:
            col = _CONSUMED_BY_KEY[key]
            consumed_counts[col] += 1
            if col not in consumed:
                consumed[col] = idx
        elif key in _DISPLAY_BY_KEY:
            col = _DISPLAY_BY_KEY[key]
            if col not in display:
                display[col] = idx
            else:
                ignored_labels.append(label)
        else:
            ignored_labels.append(label)

    duplicated = [col for col in CONSUMED_COLUMNS if consumed_counts[col] > 1]
    if duplicated:
        return ConsumedColumnDuplicated(duplicated=duplicated)

    missing = [col for col in CONSUMED_COLUMNS if consumed_counts[col] == 0]
    if missing:
        return ConsumedColumnMissing(missing=missing)

    return BomColumnMap(
        consumed=consumed,
        display=display,
        ignored_labels=ignored_labels,
    )


# ---- Cleaning functions (§4.3–§4.7) ----------------------------------------

def unwrap_enclosing_quotes(text: str) -> QuoteUnwrap:
    """Removes one pair of quote marks that encloses an entire value."""
    trimmed = text.strip()
    if len(trimmed) < 2:
        return QuoteUnwrap(value=trimmed, was_unwrapped=False)
    closer = QUOTE_PAIRS.get(trimmed[0])
    if closer is None or trimmed[-1] != closer:
        return QuoteUnwrap(value=trimmed, was_unwrapped=False)
    inner = trimmed[1:-1]
    if closer in inner.replace(closer + closer, ""):
        return QuoteUnwrap(value=trimmed, was_unwrapped=False)
    return QuoteUnwrap(
        value=inner.replace(closer + closer, closer).strip(),
        was_unwrapped=True,
    )


def extract_paired_notes(text: str) -> NotesExtraction:
    """Removes paired note markers and returns what they held."""
    notes: list[str] = []
    for match in _PAIRED_NOTE_RE.finditer(text):
        inner = next(g for g in match.groups() if g is not None).strip()
        if inner:
            notes.append(inner)
    remainder = _PAIRED_NOTE_RE.sub("", text)
    return NotesExtraction(remainder=remainder, notes=notes)


def is_designator_list(text: str) -> bool:
    """Whether a piece of text, read as a comma-separated list, is designators."""
    tokens = [unwrap_enclosing_quotes(tok).value for tok in text.split(",")]
    non_empty = [tok for tok in tokens if tok]
    if not non_empty:
        return False
    if not all(REFDES_TOKEN_REGEX.match(tok) for tok in non_empty):
        return False
    return any(DESIGNATOR_SHAPE.match(tok) for tok in non_empty)


def is_free_text(text: str) -> bool:
    """Whether a Ref_Des value is a sentence rather than designators."""
    if "," in text:
        return False
    if not any(ch.isspace() for ch in text):
        return False
    return not any(DESIGNATOR_SHAPE.match(word) for word in text.split())


def cut_at_note_separator(text: str) -> SeparatorCut:
    """Ends the designator part of a value at the first separator that introduces a note."""
    kept = ""
    remaining = text
    while True:
        sep = NOTE_SEPARATOR.search(remaining)
        if sep is None:
            return SeparatorCut(head=kept + remaining, note=None)
        before = remaining[:sep.start()]
        after = remaining[sep.end():]
        if after.strip() == "":
            return SeparatorCut(head=kept + before, note=None)
        next_sep = NOTE_SEPARATOR.search(after)
        segment = after[:next_sep.start()] if next_sep is not None else after
        if not is_designator_list(segment):
            return SeparatorCut(head=kept + before, note=after.strip())
        kept = kept + before + ", "
        remaining = after


def split_ref_des(text: str) -> RefDesSplit | TokenRejected:
    """Splits a cleaned Ref_Des value into designators and the notes written around them."""
    paired = extract_paired_notes(text)
    notes = list(paired.notes)
    head = paired.remainder
    star = head.find("*")
    if star != -1:
        tail = head[star + 1:].strip()
        if tail != "":
            notes.append(tail)
        head = head[:star]
    cut = cut_at_note_separator(head)
    if cut.note is not None:
        notes.append(cut.note)
    head = " ".join(cut.head.split()).strip(" ,")
    if head == "":
        return RefDesSplit(
            designators=[],
            repeats=[],
            notes=notes,
            kind=RefDesKind.EMPTY,
            residual=None,
        )
    if head.casefold() in DESIGNATOR_PLACEHOLDERS:
        return RefDesSplit(
            designators=[],
            repeats=[],
            notes=notes,
            kind=RefDesKind.PLACEHOLDER,
            residual=head,
        )
    if is_free_text(head):
        return RefDesSplit(
            designators=[],
            repeats=[],
            notes=notes,
            kind=RefDesKind.FREE_TEXT,
            residual=head,
        )
    designators: list[RefDes] = []
    repeats: list[RefDes] = []
    for token in head.split(","):
        candidate = unwrap_enclosing_quotes(token).value
        if candidate == "":
            continue
        if not REFDES_TOKEN_REGEX.match(candidate):
            return TokenRejected(token=candidate)
        if candidate in designators:
            repeats.append(candidate)
            continue
        designators.append(candidate)
    return RefDesSplit(
        designators=designators,
        repeats=repeats,
        notes=notes,
        kind=RefDesKind.DESIGNATORS,
        residual=None,
    )


def split_part_number(text: str) -> PartNumberSplit:
    """Separates a note written after a part number."""
    match = re.search(r"\s+\*", text)
    if match is None:
        return PartNumberSplit(part_number=text.strip(), note=None)
    part_number = text[:match.start()].strip()
    note_text = text[match.end():].strip()
    return PartNumberSplit(
        part_number=part_number,
        note=note_text if note_text != "" else None,
    )


def mount_type_of(text: str) -> MountType | None:
    """Reads a mount-type cell the way the shipped parser does."""
    cleaned = text.strip().upper()
    if cleaned.startswith("S"):
        return MountType.SMT
    if cleaned.startswith("T"):
        return MountType.THT
    return None


def struck_action(
    sheet_row: int,
    column: ColumnName,
    struck_text: str,
    cleaned_value: str,
) -> CleaningAction:
    """Names what a struck run removed from one cell, judged on the cell's fully cleaned value."""
    struck_clean = struck_text.strip()
    if column == ColumnName.PART_NUMBER:
        return PartNumberReplacedByStrike(
            sheet_row=sheet_row,
            struck=struck_clean,
            kept=cleaned_value,
        )
    if column == ColumnName.MOUNT_TYPE:
        mt = mount_type_of(cleaned_value)
        if mt is not None:
            return MountTypeReplacedByStrike(
                sheet_row=sheet_row,
                struck=struck_clean,
                kept=mt,
            )
        return StruckTextRemoved(sheet_row=sheet_row, column=column)
    if column == ColumnName.REF_DES:
        struck_designators: list[RefDes] = []
        for raw_tok in struck_text.split(","):
            tok = unwrap_enclosing_quotes(raw_tok).value
            if tok and REFDES_TOKEN_REGEX.match(tok) and DESIGNATOR_SHAPE.match(tok):
                struck_designators.append(tok)
        if struck_designators:
            return DesignatorsStruck(sheet_row=sheet_row, struck=struck_designators)
        return StruckTextRemoved(sheet_row=sheet_row, column=column)
    return StruckTextRemoved(sheet_row=sheet_row, column=column)


def clean_row(
    cells: Sequence[SheetCell | Any],
    sheet_row: int,
    column_map: BomColumnMap,
) -> CleanedRow | DesignatorRejected:
    """Applies the Phase 53 rules to one sheet row."""
    readings = read_columns(cells, column_map)
    part = readings[ColumnName.PART_NUMBER]
    if part.raw_text.strip() != "" and part.visible_text.strip() == "":
        return CleanedRow(
            sheet_row=sheet_row,
            cells={},
            designators=[],
            notes=[],
            is_removed_line=True,
            actions=[LineRemovedByStrike(sheet_row=sheet_row, struck_part_number=part.struck_text.strip())],
        )

    actions: list[CleaningAction] = []
    cleaned: dict[ColumnName, str] = {}
    for column, reading in readings.items():
        unwrapped = unwrap_enclosing_quotes(reading.visible_text)
        if unwrapped.was_unwrapped:
            actions.append(EnclosingQuotesRemoved(sheet_row=sheet_row, column=column))
        cleaned[column] = unwrapped.value

    find_text = cleaned[ColumnName.FIND_NUMBER]
    notes: list[BomNote] = []
    part_split = split_part_number(cleaned[ColumnName.PART_NUMBER])
    cleaned[ColumnName.PART_NUMBER] = part_split.part_number
    if part_split.note is not None:
        notes.append(
            BomNote(
                sheet_row=sheet_row,
                find_number_text=find_text,
                column=ColumnName.PART_NUMBER,
                text=part_split.note,
            )
        )

    ref_des = split_ref_des(cleaned[ColumnName.REF_DES])
    if isinstance(ref_des, TokenRejected):
        return DesignatorRejected(
            sheet_row=sheet_row,
            cell_text=cleaned[ColumnName.REF_DES],
            token=ref_des.token,
            find_number_text=find_text,
            cells=cleaned,
            notes=notes,
        )

    for column, reading in readings.items():
        if reading.struck_text.strip() != "":
            actions.append(
                struck_action(
                    sheet_row=sheet_row,
                    column=column,
                    struck_text=reading.struck_text,
                    cleaned_value=cleaned[column],
                )
            )

    for text in ref_des.notes:
        notes.append(
            BomNote(
                sheet_row=sheet_row,
                find_number_text=find_text,
                column=ColumnName.REF_DES,
                text=text,
            )
        )
    for note in notes:
        actions.append(NoteExtracted(note=note))
    for repeat in ref_des.repeats:
        actions.append(DesignatorRepeatDropped(sheet_row=sheet_row, designator=repeat))
    if ref_des.kind == RefDesKind.PLACEHOLDER and ref_des.residual is not None:
        actions.append(DesignatorPlaceholder(sheet_row=sheet_row, text=ref_des.residual))
    if ref_des.kind == RefDesKind.FREE_TEXT and ref_des.residual is not None:
        actions.append(FreeTextInRefDes(sheet_row=sheet_row, text=ref_des.residual))
    if ref_des.residual is not None:
        notes.append(
            BomNote(
                sheet_row=sheet_row,
                find_number_text=find_text,
                column=ColumnName.REF_DES,
                text=ref_des.residual,
            )
        )

    return CleanedRow(
        sheet_row=sheet_row,
        cells=cleaned,
        designators=ref_des.designators,
        notes=notes,
        is_removed_line=False,
        actions=actions,
    )


# ---- Report & Summary assembly (§4.7, §5.1, §8.1) --------------------------

def mount_gate_actions(sheet: CleanedSheet) -> list[CleaningAction]:
    """The skip reports the mount-type gate produces, without applying any other gate."""
    actions: list[CleaningAction] = []
    for row_res in sheet.rows:
        if isinstance(row_res, DesignatorRejected):
            continue
        if row_res.is_removed_line:
            continue
        mount_text = row_res.cells.get(ColumnName.MOUNT_TYPE, "").strip()
        if mount_type_of(mount_text) is not None:
            continue
        part_number = row_res.cells.get(ColumnName.PART_NUMBER, "").strip()
        if not part_number:
            continue
        if part_number.upper() in SILENT_MOUNT_TEXTS:
            continue
        if mount_text.upper() in SILENT_MOUNT_TEXTS:
            continue
        find_text = row_res.cells.get(ColumnName.FIND_NUMBER, "").strip()
        if not find_text and not row_res.designators:
            continue
        actions.append(
            UnknownMountTypeSkipped(
                sheet_row=row_res.sheet_row,
                part_number=part_number,
                mount_text=mount_text,
            )
        )
    return actions


def build_cleaning_report(
    sheet: CleanedSheet,
    gate_actions: Sequence[CleaningAction],
) -> CleaningReport:
    """Collects one sheet's cleaning into a report."""
    actions: list[CleaningAction] = []
    if not sheet.sheet.is_visible:
        actions.append(HiddenSheetRead(sheet_name=sheet.sheet.sheet_name))

    gate_by_row: dict[int, list[CleaningAction]] = {}
    unattached_gates: list[CleaningAction] = []
    for ga in gate_actions:
        row_num = getattr(ga, "sheet_row", None)
        if isinstance(row_num, int):
            gate_by_row.setdefault(row_num, []).append(ga)
        else:
            unattached_gates.append(ga)

    rejected_rows: list[DesignatorRejected] = []
    emitted_gate_rows: set[int] = set()
    for row_res in sheet.rows:
        if isinstance(row_res, DesignatorRejected):
            rejected_rows.append(row_res)
            continue
        actions.extend(row_res.actions)
        if row_res.sheet_row in gate_by_row:
            actions.extend(gate_by_row[row_res.sheet_row])
            emitted_gate_rows.add(row_res.sheet_row)

    for row_num, remaining in gate_by_row.items():
        if row_num not in emitted_gate_rows:
            actions.extend(remaining)
    actions.extend(unattached_gates)

    return CleaningReport(
        rules_version=CLEANING_RULES_VERSION,
        sheet_name=sheet.sheet.sheet_name,
        sheet_is_visible=sheet.sheet.is_visible,
        header_row=sheet.sheet.header_row,
        actions=actions,
        rejected_rows=rejected_rows,
    )


def summarise_cleaning(report: CleaningReport) -> CleaningSummary:
    """Splits a report into what you should look at and what is just counted."""
    routine_counts: dict[type, int] = {}
    consequential: list[CleaningAction] = []
    for action in report.actions:
        kind = type(action)
        if kind in CONSEQUENTIAL_ACTIONS:
            consequential.append(action)
        else:
            routine_counts[kind] = routine_counts.get(kind, 0) + 1
    return CleaningSummary(
        routine_counts=routine_counts,
        consequential=consequential,
    )


def describe_routine_cleaning(summary: CleaningSummary) -> str:
    """Phrases the routine cleaning counts for the progress line and the toast."""
    parts: list[str] = []
    for kind in ROUTINE_ORDER:
        count = summary.routine_counts.get(kind, 0)
        if count > 0:
            singular, plural = ROUTINE_PHRASES[kind]
            word = singular if count == 1 else plural
            parts.append(f"{count} {word}")
    if not parts:
        return ""
    return "cleaned: " + ", ".join(parts)


# ---- Find# Coercion --------------------------------------------------------

FIND_NUMBER_GRAMMAR = re.compile(r"^[0-9]+[A-Za-z]?$")
_WHOLE_FLOAT_STR_RE = re.compile(r"^([0-9]+)\.0+$")


def is_number(raw: Any) -> bool:
    if isinstance(raw, bool):
        return False
    return isinstance(raw, (int, float))


def text_of(raw: Any) -> str:
    return str(raw)


def leading_digits_of(candidate: str) -> int:
    m = re.match(r"^([0-9]+)", candidate)
    return int(m.group(1))


def coerce_find_number(
    raw: Any,
    path: pathlib.Path,
    mpn: str,
    sheet_row: int | None = None,
) -> str:
    base_detail: dict[str, Any] = {"mpn": mpn}
    if sheet_row is not None:
        base_detail["row"] = sheet_row

    if raw is None or text_of(raw).strip() == "":
        raise MalformedBomError(path, "MISSING_FIND_NUMBER", dict(base_detail))

    if is_number(raw):
        whole = int(raw)
        if whole != raw or whole < 1:
            raise MalformedBomError(path, "INVALID_FIND_NUMBER", {**base_detail, "raw": raw})
        return text_of(whole)

    candidate = text_of(raw).strip().upper()
    float_match = _WHOLE_FLOAT_STR_RE.match(candidate)
    if float_match is not None:
        whole_val = int(float_match.group(1))
        if whole_val < 1:
            raise MalformedBomError(path, "INVALID_FIND_NUMBER", {**base_detail, "raw": raw})
        return str(whole_val)

    if not FIND_NUMBER_GRAMMAR.match(candidate):
        raise MalformedBomError(path, "INVALID_FIND_NUMBER", {**base_detail, "raw": raw})

    if leading_digits_of(candidate) < 1:
        raise MalformedBomError(path, "INVALID_FIND_NUMBER", {**base_detail, "raw": raw})

    return candidate


# ---- Workbook opening & shared reader (§3.1, §3.4, §5) ---------------------

def _workbook_has_strike_runs(path: pathlib.Path) -> bool:
    """Cheap pre-check: does any string carry run-level strikethrough?"""
    try:
        with zipfile.ZipFile(str(path)) as zf:
            names = [
                n for n in zf.namelist()
                if n == "xl/sharedStrings.xml"
                or (n.startswith("xl/worksheets/") and n.endswith(".xml"))
            ]
            for name in names:
                if b"<strike" in zf.read(name):
                    return True
        return False
    except Exception:
        return False


def _open_workbook(path: pathlib.Path) -> openpyxl.Workbook:
    load_kwargs: dict[str, Any] = dict(
        filename=str(path),
        data_only=True,
        read_only=True,
        rich_text=True,
    )
    if _workbook_has_strike_runs(path):
        load_kwargs["read_only"] = False
    return openpyxl.load_workbook(**load_kwargs)


def read_cleaned_sheet(path: pathlib.Path) -> CleanedSheet:
    """Opens an Audit BOM and cleans every data row, without the ingest gates."""
    declared_job_number = derive_part_number_from_filename(path)

    try:
        wb = _open_workbook(path)
    except Exception as exc:
        raise MalformedBomError(path, "UNREADABLE_WORKBOOK", {"error": str(exc)})

    try:
        probes = probe_sheets(wb)
        chosen = choose_bom_sheet(probes, declared_job_number)
        if isinstance(chosen, NoSheetHasHeader):
            raise MalformedBomError(
                path,
                "NO_HEADERED_SHEET",
                {
                    "sheets": [
                        {
                            "sheet_name": p.sheet_name,
                            "is_visible": p.is_visible,
                            "observed_labels": p.observed_labels,
                        }
                        for p in chosen.probes
                    ]
                },
            )
        if isinstance(chosen, SheetsAmbiguous):
            raise MalformedBomError(
                path,
                "AMBIGUOUS_SHEET",
                {"candidates": chosen.candidates},
            )

        resolved = resolve_bom_header(chosen.observed_labels)
        if isinstance(resolved, ConsumedColumnDuplicated):
            dup_labels = [c.value for c in resolved.duplicated]
            raise MalformedBomError(
                path,
                "DUPLICATE_HEADER",
                {
                    "duplicated": dup_labels,
                    "column": dup_labels[0],
                    "label": dup_labels[0],
                    "observed": chosen.observed_labels,
                },
            )
        if isinstance(resolved, ConsumedColumnMissing):
            missing_labels = [c.value for c in resolved.missing]
            raise MalformedBomError(
                path,
                "MISSING_COLUMN",
                {
                    "missing": missing_labels,
                    "column": missing_labels[0],
                    "observed": chosen.observed_labels,
                },
            )

        optional_enums = (
            ColumnName.MSL_LEVEL,
            ColumnName.DATE_CODE,
            ColumnName.BAKED_DATE,
        )
        header_layout: HeaderLayout = (
            "canonical"
            if all(opt in resolved.display for opt in optional_enums)
            else "legacy"
        )

        ws = wb[chosen.sheet_name]
        rows: list[CleanedRow | DesignatorRejected] = []
        mapped_indices = list(resolved.consumed.values()) + list(resolved.display.values())

        for row_idx, row_cells in enumerate(
            ws.iter_rows(min_row=chosen.header_row + 1),
            start=chosen.header_row + 1,
        ):
            if (row_idx - chosen.header_row) % 200 == 0:
                time.sleep(0)

            has_any_text = False
            for col_idx in mapped_indices:
                if 0 <= col_idx < len(row_cells):
                    if read_cell(row_cells[col_idx]).raw_text.strip() != "":
                        has_any_text = True
                        break
            if not has_any_text:
                continue

            rows.append(clean_row(row_cells, row_idx, resolved))

        return CleanedSheet(
            sheet=chosen,
            column_map=resolved,
            header_layout=header_layout,
            rows=rows,
        )
    finally:
        wb.close()


def parse(path: pathlib.Path) -> BomResult:
    """Parses an Audit BOM for ingestion: the shared reader, then the ingest gates."""
    declared_part_number = derive_part_number_from_filename(path)
    sheet = read_cleaned_sheet(path)
    gate_actions = mount_gate_actions(sheet)
    cleaning_report = build_cleaning_report(sheet, gate_actions)

    items: list[BomItem] = []
    notes: list[BomNote] = []
    excluded_dni_count = 0
    excluded_pcb_count = 0
    mpn_occurrences: dict[str, list[tuple[str, int]]] = {}

    for row_res in sheet.rows:
        if isinstance(row_res, DesignatorRejected):
            raise MalformedBomError(
                path,
                "INVALID_REFDES_TOKEN",
                {
                    "row": row_res.sheet_row,
                    "find_number": row_res.find_number_text,
                    "raw": row_res.cell_text,
                    "token": row_res.token,
                },
            )

        if row_res.is_removed_line:
            continue

        part_number = row_res.cells.get(ColumnName.PART_NUMBER, "").strip()
        mount_raw = row_res.cells.get(ColumnName.MOUNT_TYPE, "").strip()
        is_dni_line = part_number.upper() == "DNI" or mount_raw.upper() == "DNI"
        if not is_dni_line:
            notes.extend(row_res.notes)

        mt = mount_type_of(mount_raw)
        if mt is None:
            continue

        if not part_number:
            continue

        if part_number.upper() == "DNI":
            excluded_dni_count += 1
            continue
        if part_number.upper() == "PCB":
            excluded_pcb_count += 1
            continue

        find_number = coerce_find_number(
            row_res.cells.get(ColumnName.FIND_NUMBER, ""),
            path,
            part_number,
            sheet_row=row_res.sheet_row,
        )

        mpn_occurrences.setdefault(part_number, []).append((find_number, row_res.sheet_row))

        items.append(
            BomItem(
                find_number=find_number,
                component_mpn=part_number,
                description=row_res.cells.get(ColumnName.DESCRIPTION, ""),
                ref_des_raw=row_res.cells.get(ColumnName.REF_DES, ""),
                ref_des_list=tuple(row_res.designators),
                mount_type="T" if mt == MountType.THT else "S",
            )
        )

    duplicates = {
        mpn: occs for mpn, occs in mpn_occurrences.items() if len(occs) > 1
    }
    if duplicates:
        raise MalformedBomError(
            path,
            "DUPLICATE_MPN",
            {
                "duplicates": list(duplicates.keys()),
                "lines": {
                    mpn: [{"find_number": fn, "row": r} for fn, r in occs]
                    for mpn, occs in duplicates.items()
                },
            },
        )

    summary = summarise_cleaning(cleaning_report)
    logger.info(
        "Audit BOM parsed: layout=%s, sheet_row_header=%d, items=%d, routine_counts=%s, consequential_rows=%s",
        sheet.header_layout,
        sheet.sheet.header_row,
        len(items),
        {k.__name__: v for k, v in summary.routine_counts.items()},
        [getattr(a, "sheet_row", None) for a in summary.consequential],
    )

    return BomResult(
        declared_part_number=declared_part_number,
        items=items,
        raw_row_count=len(sheet.rows),
        excluded_dni_count=excluded_dni_count,
        excluded_pcb_count=excluded_pcb_count,
        header_layout=sheet.header_layout,
        notes=notes,
        cleaning=cleaning_report,
        sheet_name_used=sheet.sheet.sheet_name,
        header_row=sheet.sheet.header_row,
    )


def read_raw_rows(workbook_path: pathlib.Path) -> list[RawBomRow]:
    """Lists the rows of an Audit BOM for the 2nd OPS table, cleaned but not gated."""
    sheet = read_cleaned_sheet(workbook_path)
    results: list[RawBomRow] = []

    for row_res in sheet.rows:
        if isinstance(row_res, DesignatorRejected):
            cell_map = row_res.cells
            designators: list[RefDes] = []
            row_notes = list(row_res.notes)
            sheet_row = row_res.sheet_row
        else:
            if row_res.is_removed_line:
                continue
            cell_map = row_res.cells
            designators = list(row_res.designators)
            row_notes = list(row_res.notes)
            sheet_row = row_res.sheet_row

        cells_tuple = tuple(
            cell_map.get(col, "") for col in _CANONICAL_COLUMN_ENUM_ORDER
        )
        if not any(cells_tuple):
            continue

        results.append(
            RawBomRow(
                sheet_ordinal=sheet_row,
                cells=cells_tuple,
                part_number=cell_map.get(ColumnName.PART_NUMBER, ""),
                description=cell_map.get(ColumnName.DESCRIPTION, ""),
                designators=designators,
                notes=row_notes,
            )
        )

    return results
