"""Phase 54 — Process requirements: X-ray & save images, XR, press fit, aquanox."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import html
import json
import logging
from pathlib import Path
import re
from typing import Any, Callable, Generic, Sequence, TypeVar
import zipfile

import docx

from cockpit.ingestion.labels import header_key
from cockpit.ingestion.parsers.audit_bom import (
    CLEANING_RULES_VERSION,
    CleanedRow,
    ColumnName,
    DesignatorRejected,
    RefDes,
    RefDesSplit,
    TokenRejected,
    read_cleaned_sheet,
    split_ref_des,
    unwrap_enclosing_quotes,
    extract_paired_notes,
)
from cockpit.ingestion.roles import SourceRole
from cockpit.persistence.clock import utcnow

logger = logging.getLogger(__name__)

T = TypeVar("T")
E = TypeVar("E")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


class RequirementKind(StrEnum):
    XRAY_SAVE_IMAGES = "xray_save_images"
    XRAY = "xray"
    PRESS_FIT = "press_fit"
    AQUANOX = "aquanox"


class RequirementState(StrEnum):
    DETECTED = "detected"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class UnitSource(StrEnum):
    ECO_TEXT = "eco_text"
    BOM_NOTE = "bom_note"
    BOM_DESCRIPTION = "bom_description"
    TRAVELER_WASH = "traveler_wash"


class XrayCellState(StrEnum):
    NOT_SCANNED = "not_scanned"
    BLANK = "blank"
    XR_ONLY = "xr_only"
    PENDING = "pending"
    DONE = "done"
    DONE_STALE = "done_stale"
    UNKNOWN = "unknown"


class BadgeState(StrEnum):
    DETECTED = "detected"
    ABSENT = "absent"
    UNKNOWN = "unknown"
    NOT_SCANNED = "not_scanned"


class VocabularyBox(StrEnum):
    PATTERNS = "patterns"
    SECTION_MARKERS = "section_markers"
    NEGATIONS_BEFORE = "negations_before"
    NEGATIONS_AFTER = "negations_after"


NormalisedToken = str
FileHash = str
Fingerprint = str

EXTRACTOR_VERSION: int = 1
MAX_EVIDENCE: int = 20
MAX_EXCERPT_CHARS: int = 160
MAX_DOCX_ENTRY_BYTES: int = 16_777_216
MAX_DOCX_TOTAL_BYTES: int = 67_108_864
DEFAULT_VOCABULARY_GENERATION: int = 1
WINDOW_RANGE: range = range(1, 21)
SINGLE_GROUP_WINDOW: int = 0
EVIDENCE_CACHE_ENTRIES: int = 64
MAX_PATTERNS_PER_KIND: int = 50
MAX_TERMS_PER_GROUP: int = 30
MAX_TERM_CHARS: int = 40
MAX_VOCABULARY_FILE_BYTES: int = 65_536
SECTION_PLACEHOLDER_TOKENS: list[list[NormalisedToken]] = [
    [],
    ["na"],
    ["none"],
    ["no"],
]

VOCABULARY_SETTINGS_KEY: str = "requirements/vocabulary"
VOCABULARY_GENERATION_KEY: str = "requirements/vocabulary_generation"
VOCABULARY_EXPORT_FORMAT: str = "cockpit.requirements.vocabulary"
VOCABULARY_EXPORT_VERSION: int = 1


@dataclass(frozen=True)
class Result(Generic[T, E]):
    ok: T | None = None
    err: E | None = None

    @property
    def is_ok(self) -> bool:
        return self.err is None

    @property
    def is_err(self) -> bool:
        return self.err is not None

    @property
    def value(self) -> T:
        if self.err is not None:
            raise ValueError(f"Result holds error: {self.err}")
        return self.ok  # type: ignore[return-value]

    @property
    def error(self) -> E:
        if self.err is None:
            raise ValueError("Result holds ok value")
        return self.err


def Success(value: T = None) -> Result[T, Any]:  # type: ignore[assignment]
    return Result(ok=value, err=None)


def Failure(error: E) -> Result[Any, E]:
    return Result(ok=None, err=error)


@dataclass(frozen=True)
class Token:
    text: NormalisedToken
    start: int
    end: int


@dataclass(frozen=True)
class TextUnit:
    source: UnitSource
    location: str
    text: str
    designators: tuple[RefDes, ...] = ()
    is_xray_marker: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.designators, tuple):
            object.__setattr__(self, "designators", tuple(self.designators))


@dataclass(frozen=True)
class TermGroup:
    alternatives: frozenset[NormalisedToken]

    def __post_init__(self) -> None:
        if not isinstance(self.alternatives, frozenset):
            object.__setattr__(self, "alternatives", frozenset(self.alternatives))


@dataclass(frozen=True)
class RequirementPattern:
    groups: tuple[TermGroup, ...]
    window_tokens: int = SINGLE_GROUP_WINDOW

    def __post_init__(self) -> None:
        if not isinstance(self.groups, tuple):
            object.__setattr__(self, "groups", tuple(self.groups))


@dataclass(frozen=True)
class RequirementVocabulary:
    patterns: dict[RequirementKind, tuple[RequirementPattern, ...]]
    xray_section_markers: tuple[RequirementPattern, ...]
    admitted_sources: dict[RequirementKind, frozenset[UnitSource]]
    negations_before: tuple[tuple[NormalisedToken, ...], ...]
    negations_after: tuple[tuple[NormalisedToken, ...], ...]
    generation: int = DEFAULT_VOCABULARY_GENERATION

    def __post_init__(self) -> None:
        norm_patterns = {
            RequirementKind(k): tuple(v) for k, v in self.patterns.items()
        }
        for kind in RequirementKind:
            norm_patterns.setdefault(kind, ())
        object.__setattr__(self, "patterns", norm_patterns)
        if not isinstance(self.xray_section_markers, tuple):
            object.__setattr__(self, "xray_section_markers", tuple(self.xray_section_markers))
        norm_sources = {
            RequirementKind(k): frozenset(UnitSource(s) for s in v)
            for k, v in self.admitted_sources.items()
        }
        for kind in RequirementKind:
            norm_sources.setdefault(kind, frozenset())
        object.__setattr__(self, "admitted_sources", norm_sources)
        object.__setattr__(
            self,
            "negations_before",
            tuple(tuple(p) for p in self.negations_before),
        )
        object.__setattr__(
            self,
            "negations_after",
            tuple(tuple(p) for p in self.negations_after),
        )


@dataclass(frozen=True)
class RequirementMatch:
    kind: RequirementKind
    unit: TextUnit
    first_token: int
    last_token: int
    span_tokens: tuple[NormalisedToken, ...]
    is_negated: bool

    def __post_init__(self) -> None:
        if not isinstance(self.span_tokens, tuple):
            object.__setattr__(self, "span_tokens", tuple(self.span_tokens))


@dataclass(frozen=True)
class RequirementEvidence:
    kind: RequirementKind
    source: UnitSource
    location: str
    excerpt: str
    is_negated: bool = False


@dataclass(frozen=True)
class SectionAbsent:
    pass


@dataclass(frozen=True)
class TableRows:
    live_rows: int


@dataclass(frozen=True)
class PlaceholderFollows:
    pass


@dataclass(frozen=True)
class NothingFollows:
    pass


@dataclass(frozen=True)
class Unrecognised:
    following: str


XraySection = (
    SectionAbsent | TableRows | PlaceholderFollows | NothingFollows | Unrecognised
)


@dataclass(frozen=True)
class EcoUnits:
    units: list[TextUnit]
    xray_section: XraySection
    xray_locations: set[RefDes]


@dataclass(frozen=True)
class DocumentUnreadable:
    role: SourceRole
    reason: str


@dataclass(frozen=True)
class RequirementScan:
    states: dict[RequirementKind, RequirementState]
    evidence: list[RequirementEvidence]
    unknown_reasons: dict[RequirementKind, str]
    xray_task: Fingerprint | None
    fingerprint: Fingerprint


@dataclass(frozen=True)
class RequirementSummary:
    states: dict[RequirementKind, RequirementState]
    unknown_reasons: dict[RequirementKind, str]
    xray_task: Fingerprint | None
    fingerprint: Fingerprint
    scanned_at: datetime


@dataclass(frozen=True)
class ScanInputs:
    notes_path: Path
    notes_hash: FileHash
    bom_path: Path
    bom_hash: FileHash
    traveler_wash_text: str | None
    vocabulary: RequirementVocabulary


@dataclass(frozen=True)
class XrayTick:
    is_done: bool = False
    task_at_tick: Fingerprint | None = None
    ticked_at: datetime | None = None


@dataclass(frozen=True)
class RequirementBadges:
    aquanox: BadgeState = BadgeState.NOT_SCANNED
    press_fit: BadgeState = BadgeState.NOT_SCANNED
    xray_cell: XrayCellState = XrayCellState.NOT_SCANNED


@dataclass(frozen=True)
class AuditNotFoundForTick:
    audit_id: int


@dataclass(frozen=True)
class SaveImagesNotRequired:
    audit_id: int


TickRefused = AuditNotFoundForTick | SaveImagesNotRequired


@dataclass(frozen=True)
class PatternRejected:
    position: int
    reason: str


@dataclass(frozen=True)
class AddedPattern:
    kind: RequirementKind | None
    box: VocabularyBox
    line: str


@dataclass(frozen=True)
class PatternMergeOutcome:
    vocabulary: RequirementVocabulary
    added: list[AddedPattern]
    generation_advanced_to: int


# ---- §4.1 Normalisation ----------------------------------------------------

_COMPOUND_SEP = r"[ \t\u00a0\-\u2010-\u2015\u2212]*"
_WORD_LEFT = r"(?<![0-9A-Za-z])"
_WORD_RIGHT = r"(?![0-9A-Za-z]|['\u2018\u2019][A-Za-z])"

_COMPOUND_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            rf"{_WORD_LEFT}x{_COMPOUND_SEP}ray(?:s|ed|ing)?{_WORD_RIGHT}",
            re.IGNORECASE,
        ),
        "xray",
    ),
    (
        re.compile(
            rf"{_WORD_LEFT}press(?:ed)?{_COMPOUND_SEP}fit(?:s|ted|ting)?{_WORD_RIGHT}",
            re.IGNORECASE,
        ),
        "pressfit",
    ),
    (
        re.compile(
            rf"{_WORD_LEFT}aqua{_COMPOUND_SEP}nox{_WORD_RIGHT}",
            re.IGNORECASE,
        ),
        "aquanox",
    ),
)

_NA_RE = re.compile(rf"{_WORD_LEFT}n/a{_WORD_RIGHT}", re.IGNORECASE)
_APOSTROPHE_CHARS = frozenset({"'", "\u2018", "\u2019"})


def _apply_regex_with_origins(
    chars: list[str],
    origins: list[tuple[int, int]],
    pattern: re.Pattern[str],
    replacement: str,
) -> tuple[list[str], list[tuple[int, int]]]:
    text = "".join(chars)
    out_chars: list[str] = []
    out_origins: list[tuple[int, int]] = []
    cursor = 0
    for m in pattern.finditer(text):
        start, end = m.start(), m.end()
        if start > cursor:
            out_chars.extend(chars[cursor:start])
            out_origins.extend(origins[cursor:start])
        orig_span = (origins[start][0], origins[end - 1][1])
        for ch in replacement:
            out_chars.append(ch)
            out_origins.append(orig_span)
        cursor = end
    if cursor < len(chars):
        out_chars.extend(chars[cursor:])
        out_origins.extend(origins[cursor:])
    return out_chars, out_origins


def normalise(text: str) -> list[Token]:
    """Turns text into comparison tokens, keeping each token's position in the original text (§4.1)."""
    if not text:
        return []

    chars = list(text)
    origins = [(i, i + 1) for i in range(len(text))]

    # 1. Compounds are rewritten first, case-insensitively and across any run of
    #    spaces, ASCII hyphens or Unicode dashes.
    for comp_re, replacement in _COMPOUND_RULES:
        chars, origins = _apply_regex_with_origins(chars, origins, comp_re, replacement)

    # 2. Casefold, NBSP -> space.
    folded_chars: list[str] = []
    folded_origins: list[tuple[int, int]] = []
    for ch, orig in zip(chars, origins):
        if ch == "\u00a0":
            folded_chars.append(" ")
            folded_origins.append(orig)
        else:
            for c in ch.casefold():
                folded_chars.append(c)
                folded_origins.append(orig)

    # 3. An apostrophe between letters is dropped.
    no_apos_chars: list[str] = []
    no_apos_origins: list[tuple[int, int]] = []
    n = len(folded_chars)
    for i, (ch, orig) in enumerate(zip(folded_chars, folded_origins)):
        if (
            ch in _APOSTROPHE_CHARS
            and 0 < i < n - 1
            and folded_chars[i - 1].isalpha()
            and folded_chars[i + 1].isalpha()
        ):
            continue
        no_apos_chars.append(ch)
        no_apos_origins.append(orig)

    # 4. "n/a" becomes "na".
    na_chars, na_origins = _apply_regex_with_origins(
        no_apos_chars, no_apos_origins, _NA_RE, "na"
    )

    # 5. Split on every character that isn't a letter or a digit.
    tokens: list[Token] = []
    i = 0
    m = len(na_chars)
    while i < m:
        if na_chars[i].isalpha() or na_chars[i].isdigit():
            j = i + 1
            while j < m and (na_chars[j].isalpha() or na_chars[j].isdigit()):
                j += 1
            tok_text = "".join(na_chars[i:j])
            tok_start = na_origins[i][0]
            tok_end = na_origins[j - 1][1]
            tokens.append(Token(text=tok_text, start=tok_start, end=tok_end))
            i = j
        else:
            i += 1
    return tokens


# ---- §10.1 Pattern grammar -------------------------------------------------

def parse_pattern_line(line: str) -> Result[RequirementPattern, PatternRejected]:
    """Parses one line of the editor (§10.1)."""
    if not line or not line.strip():
        return Failure(PatternRejected(position=0, reason="Pattern line cannot be empty"))

    tilde_first = line.find("~")
    if tilde_first != -1:
        tilde_second = line.find("~", tilde_first + 1)
        if tilde_second != -1:
            return Failure(
                PatternRejected(position=tilde_second, reason="Only one '~' window is allowed")
            )
        groups_part = line[:tilde_first]
        window_part = line[tilde_first + 1 :]
    else:
        groups_part = line
        window_part = None

    if not groups_part.strip():
        return Failure(PatternRejected(position=0, reason="Missing pattern terms before '~'"))

    raw_groups = groups_part.split("+")
    groups: list[TermGroup] = []
    cursor = 0
    for g_idx, raw_group in enumerate(raw_groups):
        group_offset = cursor
        cursor += len(raw_group) + 1  # +1 for '+'
        if not raw_group.strip():
            return Failure(
                PatternRejected(position=group_offset, reason="Empty term group around '+'")
            )

        raw_terms = raw_group.split("|")
        if len(raw_terms) > MAX_TERMS_PER_GROUP:
            return Failure(
                PatternRejected(
                    position=group_offset,
                    reason=f"Group exceeds maximum of {MAX_TERMS_PER_GROUP} terms",
                )
            )

        alternatives: set[NormalisedToken] = set()
        t_cursor = group_offset
        for raw_term in raw_terms:
            term_offset = t_cursor
            t_cursor += len(raw_term) + 1  # +1 for '|'
            stripped = raw_term.strip()
            leading_ws = len(raw_term) - len(raw_term.lstrip())
            term_pos = term_offset + leading_ws
            if not stripped:
                return Failure(
                    PatternRejected(position=term_offset, reason="Empty term in group")
                )
            if len(stripped) > MAX_TERM_CHARS:
                return Failure(
                    PatternRejected(
                        position=term_pos,
                        reason=f"Term exceeds maximum length of {MAX_TERM_CHARS} characters",
                    )
                )
            norm_tokens = normalise(stripped)
            if len(norm_tokens) != 1:
                return Failure(
                    PatternRejected(
                        position=term_pos,
                        reason=f"Term '{stripped}' must normalise to a single token (got {len(norm_tokens)})",
                    )
                )
            alternatives.add(norm_tokens[0].text)

        if len(alternatives) > MAX_TERMS_PER_GROUP:
            return Failure(
                PatternRejected(
                    position=group_offset,
                    reason=f"Group exceeds maximum of {MAX_TERMS_PER_GROUP} terms",
                )
            )
        groups.append(TermGroup(alternatives=frozenset(alternatives)))

    if len(groups) == 1:
        if window_part is not None:
            return Failure(
                PatternRejected(
                    position=tilde_first,
                    reason="A single-group pattern cannot specify a '~' window",
                )
            )
        return Success(
            RequirementPattern(groups=tuple(groups), window_tokens=SINGLE_GROUP_WINDOW)
        )

    # Multi-group pattern requires a window in WINDOW_RANGE (1..20).
    if window_part is None:
        return Failure(
            PatternRejected(
                position=len(line),
                reason=f"Multi-group patterns require '~ N' (where N is {WINDOW_RANGE.start}..{WINDOW_RANGE.stop - 1})",
            )
        )

    win_leading = len(window_part) - len(window_part.lstrip())
    win_pos = tilde_first + 1 + win_leading
    win_stripped = window_part.strip()
    if not win_stripped or not re.fullmatch(r"-?\d+", win_stripped):
        return Failure(
            PatternRejected(
                position=win_pos,
                reason=f"Window must be an integer in {WINDOW_RANGE.start}..{WINDOW_RANGE.stop - 1}",
            )
        )
    win_val = int(win_stripped)
    if win_val not in WINDOW_RANGE:
        return Failure(
            PatternRejected(
                position=win_pos,
                reason=f"Window must be between {WINDOW_RANGE.start} and {WINDOW_RANGE.stop - 1}",
            )
        )

    return Success(RequirementPattern(groups=tuple(groups), window_tokens=win_val))


def render_pattern_line(pattern: RequirementPattern) -> str:
    """Renders a pattern back into the editor's syntax (§10.1)."""
    group_strs = [
        " | ".join(sorted(group.alternatives))
        for group in pattern.groups
    ]
    joined = " + ".join(group_strs)
    if len(pattern.groups) <= 1:
        return joined
    return f"{joined} ~ {pattern.window_tokens}"


def parse_negation_line(line: str) -> Result[tuple[NormalisedToken, ...], PatternRejected]:
    """Parses one negation phrase line (words separated by spaces, normalised as document text)."""
    if not line or not line.strip():
        return Failure(PatternRejected(position=0, reason="Negation line cannot be empty"))
    for bad_ch in ("+", "|", "~"):
        pos = line.find(bad_ch)
        if pos != -1:
            return Failure(
                PatternRejected(
                    position=pos,
                    reason=f"Negation phrase cannot contain '{bad_ch}'",
                )
            )
    if len(line.strip()) > MAX_TERM_CHARS * 2:
        return Failure(
            PatternRejected(
                position=0,
                reason="Negation phrase is too long",
            )
        )
    tokens = normalise(line)
    if not tokens:
        return Failure(
            PatternRejected(
                position=0,
                reason="Negation phrase must contain at least one word",
            )
        )
    if len(tokens) > MAX_TERMS_PER_GROUP:
        return Failure(
            PatternRejected(
                position=0,
                reason=f"Negation phrase exceeds {MAX_TERMS_PER_GROUP} words",
            )
        )
    for tok in tokens:
        if len(tok.text) > MAX_TERM_CHARS:
            return Failure(
                PatternRejected(
                    position=tok.start,
                    reason=f"Word exceeds {MAX_TERM_CHARS} characters",
                )
            )
    return Success(tuple(t.text for t in tokens))


def render_negation_line(phrase: Sequence[NormalisedToken]) -> str:
    """Renders a normalised negation phrase back into editor syntax."""
    return " ".join(phrase)


# ---- §4.2, §4.3 Matching and Negation --------------------------------------

def is_negated(
    tokens: Sequence[Token],
    first: int,
    last: int,
    vocabulary: RequirementVocabulary,
) -> bool:
    """Whether a match is negated by the words directly around it (§4.3)."""
    for phrase in vocabulary.negations_before:
        k = len(phrase)
        if k > 0 and first >= k:
            if tuple(tokens[i].text for i in range(first - k, first)) == phrase:
                return True
    for phrase in vocabulary.negations_after:
        k = len(phrase)
        if k > 0 and last + k < len(tokens):
            if tuple(tokens[i].text for i in range(last + 1, last + 1 + k)) == phrase:
                return True
    return False


def _can_assign_distinct_positions(
    group_positions: list[list[int]],
    used: set[int],
    group_idx: int,
) -> bool:
    if group_idx == len(group_positions):
        return True
    for pos in group_positions[group_idx]:
        if pos not in used:
            used.add(pos)
            if _can_assign_distinct_positions(group_positions, used, group_idx + 1):
                return True
            used.remove(pos)
    return False


def find_matches(
    tokens: Sequence[Token],
    pattern: RequirementPattern,
    vocabulary: RequirementVocabulary | None = None,
) -> list[tuple[int, int]]:
    """Finds every match of one pattern in one unit's tokens (§4.2)."""
    num_groups = len(pattern.groups)
    if num_groups == 0 or not tokens:
        return []

    if num_groups == 1:
        alts = pattern.groups[0].alternatives
        return [(i, i) for i, tok in enumerate(tokens) if tok.text in alts]

    window = pattern.window_tokens
    n = len(tokens)
    # Precompute which groups each token position satisfies.
    token_groups: list[list[int]] = [
        [g_idx for g_idx, g in enumerate(pattern.groups) if tok.text in g.alternatives]
        for tok in tokens
    ]

    candidates: list[tuple[int, int, int]] = []  # (spread, first, last)
    for first in range(n):
        if not token_groups[first]:
            continue
        max_last = min(n - 1, first + window)
        for last in range(first, max_last + 1):
            if not token_groups[last]:
                continue
            if last - first + 1 < num_groups:
                continue
            # Check if every group has at least one matching position in [first, last].
            positions_per_group: list[list[int]] = [[] for _ in range(num_groups)]
            for pos in range(first, last + 1):
                for g_idx in token_groups[pos]:
                    positions_per_group[g_idx].append(pos)
            if any(not pos_list for pos_list in positions_per_group):
                continue
            # Sort smaller domain groups first for fast backtracking.
            positions_per_group.sort(key=len)
            if _can_assign_distinct_positions(positions_per_group, set(), 0):
                candidates.append((last - first, first, last))

    if not candidates:
        return []

    # Shortest span wins, then leftmost.
    candidates.sort()
    chosen: list[tuple[int, int]] = []
    for _spread, first, last in candidates:
        if any(first <= c_last and c_first <= last for c_first, c_last in chosen):
            continue
        chosen.append((first, last))

    chosen.sort()
    return chosen


# ---- §4.5 Default Vocabulary -----------------------------------------------

def _must_parse_pattern(line: str) -> RequirementPattern:
    res = parse_pattern_line(line)
    assert res.is_ok, f"Default pattern failed to parse: {line} -> {res.err}"
    return res.value


DEFAULT_VOCABULARY = RequirementVocabulary(
    patterns={
        RequirementKind.XRAY_SAVE_IMAGES: (
            _must_parse_pattern(
                "xray + save | saving | saved | saves + "
                "image | images | picture | pictures | photo | photos | photograph | photographs ~ 8"
            ),
        ),
        RequirementKind.XRAY: (
            _must_parse_pattern("xray"),
        ),
        RequirementKind.PRESS_FIT: (
            _must_parse_pattern("pressfit"),
        ),
        RequirementKind.AQUANOX: (
            _must_parse_pattern("aquanox"),
            _must_parse_pattern("chemical | chem + wash | washing | washed ~ 2"),
            _must_parse_pattern("solvent + wash | washing | washed ~ 2"),
        ),
    },
    xray_section_markers=(
        _must_parse_pattern("xray + following + locations ~ 6"),
    ),
    admitted_sources={
        RequirementKind.XRAY_SAVE_IMAGES: frozenset({
            UnitSource.ECO_TEXT,
            UnitSource.BOM_NOTE,
        }),
        RequirementKind.XRAY: frozenset({
            UnitSource.ECO_TEXT,
            UnitSource.BOM_NOTE,
        }),
        RequirementKind.PRESS_FIT: frozenset({
            UnitSource.ECO_TEXT,
            UnitSource.BOM_NOTE,
            UnitSource.BOM_DESCRIPTION,
        }),
        RequirementKind.AQUANOX: frozenset({
            UnitSource.ECO_TEXT,
            UnitSource.BOM_NOTE,
            UnitSource.TRAVELER_WASH,
        }),
    },
    negations_before=(
        ("no",),
        ("not",),
        ("do", "not"),
        ("dont",),
        ("without",),
        ("never",),
    ),
    negations_after=(
        ("not", "required"),
        ("not", "needed"),
        ("na",),
        ("none",),
        ("no",),
    ),
    generation=DEFAULT_VOCABULARY_GENERATION,
)

SHIPPED_VOCABULARY_BY_GENERATION: dict[int, RequirementVocabulary] = {
    1: DEFAULT_VOCABULARY,
}


# ---- §3.1 Reading the ECO --------------------------------------------------

def check_docx_package(notes_path: Path) -> Result[None, DocumentUnreadable]:
    """Refuses a .docx whose package would decompress beyond the caps (§3.1)."""
    try:
        notes_path = Path(notes_path)
        if not notes_path.is_file():
            return Failure(DocumentUnreadable(SourceRole.NOTES, "ECO file is missing"))
        with zipfile.ZipFile(notes_path, "r") as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        return Failure(DocumentUnreadable(SourceRole.NOTES, f"BadZipFile: {exc}"))
    except OSError as exc:
        return Failure(DocumentUnreadable(SourceRole.NOTES, f"{type(exc).__name__}: {exc}"))
    except Exception as exc:
        return Failure(DocumentUnreadable(SourceRole.NOTES, f"{type(exc).__name__}: {exc}"))

    total_bytes = 0
    for info in infos:
        size = info.file_size
        if size < 0 or size > MAX_DOCX_ENTRY_BYTES:
            return Failure(
                DocumentUnreadable(
                    SourceRole.NOTES,
                    f"Entry '{info.filename}' exceeds MAX_DOCX_ENTRY_BYTES ({size} > {MAX_DOCX_ENTRY_BYTES})",
                )
            )
        total_bytes += size
        if total_bytes > MAX_DOCX_TOTAL_BYTES:
            return Failure(
                DocumentUnreadable(
                    SourceRole.NOTES,
                    f"Package uncompressed total exceeds MAX_DOCX_TOTAL_BYTES ({total_bytes} > {MAX_DOCX_TOTAL_BYTES})",
                )
            )
    return Success(None)


def _rpr_has_strike(rpr_elem: Any) -> bool | None:
    if rpr_elem is None:
        return None
    for tag in ("strike", "dstrike"):
        el = rpr_elem.find(_w(tag))
        if el is not None:
            val = el.get(_w("val"))
            if val is None or val.strip().lower() not in ("0", "false", "none", "off"):
                return True
            return False
    return None


def _is_run_struck(r_elem: Any, p_struck: bool = False) -> bool:
    r_strike = _rpr_has_strike(r_elem.find(_w("rPr")))
    if r_strike is not None:
        return r_strike
    return p_struck


def _paragraph_default_struck(p_elem: Any) -> bool:
    ppr = p_elem.find(_w("pPr"))
    if ppr is None:
        return False
    return bool(_rpr_has_strike(ppr.find(_w("rPr"))))


_EXCLUDED_CONTAINERS = frozenset({
    _w("del"),
    _w("moveFrom"),
    _w("instrText"),
    f"{{{MC_NS}}}Fallback",
})

_IMAGE_TAGS = frozenset({
    _w("drawing"),
    _w("pict"),
    _w("object"),
})


def _extract_run_text(r_elem: Any) -> str:
    parts: list[str] = []
    for child in r_elem:
        tag = child.tag
        if tag in _EXCLUDED_CONTAINERS:
            continue
        local = tag.split("}")[-1] if "}" in tag else tag
        if local == "t":
            if child.text:
                parts.append(child.text)
        elif local == "noBreakHyphen":
            parts.append("\u2011")
        elif local in ("tab", "ptab"):
            parts.append("\t")
        elif local in ("br", "cr"):
            parts.append("\n")
    return "".join(parts)


def _extract_paragraph_text_and_textboxes(
    p_elem: Any,
) -> tuple[str, list[str], bool]:
    """Returns (paragraph_live_text, [textbox_texts...], has_image_outside_textbox)."""
    p_struck = _paragraph_default_struck(p_elem)
    run_parts: list[str] = []
    textboxes: list[str] = []
    has_image = False

    def _walk_in_p(node: Any) -> None:
        nonlocal has_image
        for child in node:
            tag = child.tag
            if tag in _EXCLUDED_CONTAINERS:
                continue
            local = tag.split("}")[-1] if "}" in tag else tag
            if local == "txbxContent":
                tb_lines: list[str] = []
                for tb_p in child.findall(f".//{_w('p')}"):
                    # Ensure tb_p is not inside a del/moveFrom
                    tb_text, _, _ = _extract_paragraph_text_and_textboxes(tb_p)
                    if tb_text.strip():
                        tb_lines.append(tb_text.strip())
                if tb_lines:
                    textboxes.append("\n".join(tb_lines))
                continue
            if tag in _IMAGE_TAGS:
                # If this drawing/pict holds a textbox, walk it to extract the textbox;
                # otherwise it's an embedded image.
                if child.find(f".//{_w('txbxContent')}") is not None:
                    _walk_in_p(child)
                else:
                    has_image = True
                continue
            if local == "r":
                if not _is_run_struck(child, p_struck):
                    run_parts.append(_extract_run_text(child))
                # A run may also hold a drawing with a textbox or image.
                for r_child in child:
                    if r_child.tag in _EXCLUDED_CONTAINERS:
                        continue
                    if r_child.tag == _w("txbxContent"):
                        for tb_p in r_child.findall(f".//{_w('p')}"):
                            tb_text, _, _ = _extract_paragraph_text_and_textboxes(tb_p)
                            if tb_text.strip():
                                textboxes.append(tb_text.strip())
                    elif r_child.find(f".//{_w('txbxContent')}") is not None:
                        _walk_in_p(r_child)
                    elif r_child.tag in _IMAGE_TAGS and not _is_run_struck(child, p_struck):
                        has_image = True
            else:
                _walk_in_p(child)

    _walk_in_p(p_elem)
    return "".join(run_parts), textboxes, has_image


def _tc_is_vmerge_continue(tc_elem: Any) -> bool:
    tc_pr = tc_elem.find(_w("tcPr"))
    if tc_pr is None:
        return False
    vmerge = tc_pr.find(_w("vMerge"))
    if vmerge is None:
        return False
    val = vmerge.get(_w("val"))
    return val is None or val.strip().lower() == "continue"


def _extract_cell_text_and_textboxes(tc_elem: Any) -> tuple[str, list[str], bool]:
    """Extracts live text of a table cell (nested tables join their cell) and any text boxes."""
    para_lines: list[str] = []
    textboxes: list[str] = []
    has_image = False

    def _walk_tc(node: Any) -> None:
        nonlocal has_image
        for child in node:
            tag = child.tag
            if tag in _EXCLUDED_CONTAINERS:
                continue
            local = tag.split("}")[-1] if "}" in tag else tag
            if local == "p":
                p_text, p_tbs, p_img = _extract_paragraph_text_and_textboxes(child)
                if p_text.strip():
                    para_lines.append(p_text.strip())
                textboxes.extend(p_tbs)
                if p_img:
                    has_image = True
            elif local in ("tbl", "tr", "tc", "sdt", "sdtContent", "ins", "moveTo"):
                _walk_tc(child)
            else:
                _walk_tc(child)

    _walk_tc(tc_elem)
    return "\n".join(para_lines), textboxes, has_image


def _table_rows_elements(tbl_elem: Any) -> list[Any]:
    return [child for child in tbl_elem if child.tag == _w("tr")]


def _is_xray_table_element(tbl_elem: Any) -> bool:
    rows = _table_rows_elements(tbl_elem)
    if not rows:
        return False
    first_row_tcs = [c for c in rows[0] if c.tag == _w("tc")]
    if not first_row_tcs:
        return False
    first_cell_text, _, _ = _extract_cell_text_and_textboxes(first_row_tcs[0])
    return header_key(first_cell_text) == "find#"


def classify_xray_section(
    body: Sequence[Any],
    marker_index: int | None,
) -> tuple[XraySection, set[RefDes]]:
    """Reads what follows an ECO's X-ray section marker (§3.1)."""
    xray_locations: set[RefDes] = set()

    # Check if an X-ray table (first header cell's header_key == "find#") exists anywhere in body.
    for elem in body:
        if elem.tag == _w("tbl") and _is_xray_table_element(elem):
            rows = _table_rows_elements(elem)
            header_tcs = [c for c in rows[0] if c.tag == _w("tc")]
            ref_des_col: int | None = None
            for col_idx, tc in enumerate(header_tcs):
                cell_text, _, _ = _extract_cell_text_and_textboxes(tc)
                if header_key(cell_text) == "refdes":
                    ref_des_col = col_idx
                    break

            live_rows = 0
            for tr in rows[1:]:
                tcs = [c for c in tr if c.tag == _w("tc")]
                row_texts: list[str] = []
                for tc in tcs:
                    if _tc_is_vmerge_continue(tc):
                        row_texts.append("")
                    else:
                        ct, _, _ = _extract_cell_text_and_textboxes(tc)
                        row_texts.append(ct.strip())
                if not any(row_texts):
                    continue
                live_rows += 1
                if ref_des_col is not None and ref_des_col < len(row_texts):
                    raw_ref = unwrap_enclosing_quotes(row_texts[ref_des_col]).value
                    split_res = split_ref_des(raw_ref)
                    if isinstance(split_res, RefDesSplit):
                        xray_locations.update(split_res.designators)
            return TableRows(live_rows=live_rows), xray_locations

    if marker_index is None:
        return SectionAbsent(), set()

    # No X-ray table exists and a marker is present: inspect the next non-empty body element.
    for elem in body[marker_index + 1 :]:
        tag = elem.tag
        if tag in _EXCLUDED_CONTAINERS:
            continue
        if tag == _w("p"):
            p_text, p_tbs, p_img = _extract_paragraph_text_and_textboxes(elem)
            stripped = p_text.strip()
            if not stripped and not p_tbs and not p_img:
                continue
            if p_img or p_tbs:
                excerpt = (stripped or (p_tbs[0] if p_tbs else "[image]"))[:MAX_EXCERPT_CHARS]
                return Unrecognised(following=excerpt), set()
            tok_texts = [t.text for t in normalise(stripped)]
            if tok_texts in SECTION_PLACEHOLDER_TOKENS:
                return PlaceholderFollows(), set()
            return Unrecognised(following=stripped[:MAX_EXCERPT_CHARS]), set()
        if tag == _w("tbl"):
            rows = _table_rows_elements(elem)
            cell_excerpts: list[str] = []
            has_any_img = False
            for tr in rows:
                for tc in tr:
                    if tc.tag == _w("tc") and not _tc_is_vmerge_continue(tc):
                        ct, tbs, img = _extract_cell_text_and_textboxes(tc)
                        if ct.strip():
                            cell_excerpts.append(ct.strip())
                        cell_excerpts.extend(tbs)
                        if img:
                            has_any_img = True
            if not cell_excerpts and not has_any_img:
                continue
            summary = (cell_excerpts[0] if cell_excerpts else "[table]")[:MAX_EXCERPT_CHARS]
            return Unrecognised(following=summary), set()

    return NothingFollows(), set()


def read_eco_units(
    notes_path: Path,
    vocabulary: RequirementVocabulary = DEFAULT_VOCABULARY,
) -> Result[EcoUnits, DocumentUnreadable]:
    """Lists the live text of a stored ECO as units, and reads its X-ray section (§3.1)."""
    pkg_check = check_docx_package(notes_path)
    if pkg_check.is_err:
        return Failure(pkg_check.error)

    try:
        doc = docx.Document(str(notes_path))
        body_elem = doc.element.body
        if body_elem is None:
            return Failure(DocumentUnreadable(SourceRole.NOTES, "Document has no body"))
        body_children = list(body_elem)
    except Exception as exc:
        return Failure(
            DocumentUnreadable(SourceRole.NOTES, f"{type(exc).__name__}: {exc}")
        )

    units: list[TextUnit] = []
    marker_index: int | None = None
    para_counter = 0
    table_counter = 0
    textbox_counter = 0

    try:
        for body_idx, child in enumerate(body_children):
            tag = child.tag
            if tag in _EXCLUDED_CONTAINERS:
                continue
            if tag == _w("p"):
                p_text, p_tbs, _ = _extract_paragraph_text_and_textboxes(child)
                stripped = p_text.strip()
                if stripped:
                    para_counter += 1
                    is_marker = False
                    if marker_index is None and vocabulary.xray_section_markers:
                        toks = normalise(stripped)
                        for pat in vocabulary.xray_section_markers:
                            if find_matches(toks, pat, vocabulary):
                                is_marker = True
                                marker_index = body_idx
                                break
                    units.append(
                        TextUnit(
                            source=UnitSource.ECO_TEXT,
                            location=f"ECO paragraph {para_counter}",
                            text=stripped,
                            designators=(),
                            is_xray_marker=is_marker,
                        )
                    )
                for tb_text in p_tbs:
                    textbox_counter += 1
                    units.append(
                        TextUnit(
                            source=UnitSource.ECO_TEXT,
                            location=f"ECO text box {textbox_counter}",
                            text=tb_text,
                            designators=(),
                            is_xray_marker=False,
                        )
                    )
            elif tag == _w("tbl"):
                table_counter += 1
                # Exclude the X-ray table from EcoUnits.units; it is handled structurally
                # via classify_xray_section.
                if _is_xray_table_element(child):
                    continue
                rows = _table_rows_elements(child)
                for row_idx, tr in enumerate(rows, 1):
                    tcs = [c for c in tr if c.tag == _w("tc")]
                    for col_idx, tc in enumerate(tcs, 1):
                        if _tc_is_vmerge_continue(tc):
                            continue
                        cell_text, cell_tbs, _ = _extract_cell_text_and_textboxes(tc)
                        if cell_text.strip():
                            units.append(
                                TextUnit(
                                    source=UnitSource.ECO_TEXT,
                                    location=f"ECO table {table_counter}, row {row_idx}, col {col_idx}",
                                    text=cell_text.strip(),
                                    designators=(),
                                    is_xray_marker=False,
                                )
                            )
                        for tb_text in cell_tbs:
                            textbox_counter += 1
                            units.append(
                                TextUnit(
                                    source=UnitSource.ECO_TEXT,
                                    location=f"ECO text box {textbox_counter}",
                                    text=tb_text,
                                    designators=(),
                                    is_xray_marker=False,
                                )
                            )

        xray_section, xray_locations = classify_xray_section(body_children, marker_index)
    except Exception as exc:
        return Failure(
            DocumentUnreadable(SourceRole.NOTES, f"{type(exc).__name__}: {exc}")
        )

    return Success(
        EcoUnits(
            units=units,
            xray_section=xray_section,
            xray_locations=xray_locations,
        )
    )


# ---- §3.2, §14.1 Reading the BOM -------------------------------------------

def is_dni_line(row: CleanedRow | DesignatorRejected) -> bool:
    """Whether a cleaned BOM row is a DNI line, whose notes don't apply (§14.1)."""
    part_num = row.cells.get(ColumnName.PART_NUMBER, "").strip().upper()
    mount_txt = row.cells.get(ColumnName.MOUNT_TYPE, "").strip().upper()
    return part_num == "DNI" or mount_txt == "DNI"


def read_bom_units(bom_path: Path) -> Result[list[TextUnit], DocumentUnreadable]:
    """Lists a stored Audit BOM's notes and descriptions as units (§3.2)."""
    try:
        sheet = read_cleaned_sheet(Path(bom_path))
    except Exception as exc:
        reason = getattr(exc, "reason", None) or f"{type(exc).__name__}: {exc}"
        return Failure(DocumentUnreadable(SourceRole.BOM, str(reason)))

    units: list[TextUnit] = []
    for row in sheet.rows:
        if isinstance(row, CleanedRow):
            if row.is_removed_line:
                continue
            if is_dni_line(row):
                continue
            designators = tuple(row.designators)
            find_text = row.cells.get(ColumnName.FIND_NUMBER, "").strip()
            desc_text = row.cells.get(ColumnName.DESCRIPTION, "").strip()
        else:
            if is_dni_line(row):
                continue
            designators = ()
            find_text = row.find_number_text.strip()
            desc_text = row.cells.get(ColumnName.DESCRIPTION, "").strip()

        loc = f"BOM row {row.sheet_row}, Find# {find_text}"
        existing_note_texts: set[str] = set()
        for note in row.notes:
            if note.text.strip():
                existing_note_texts.add(note.text.strip())
                note_loc = f"BOM row {note.sheet_row}, Find# {note.find_number_text.strip()}"
                units.append(
                    TextUnit(
                        source=UnitSource.BOM_NOTE,
                        location=note_loc,
                        text=note.text.strip(),
                        designators=designators,
                        is_xray_marker=False,
                    )
                )
        if isinstance(row, DesignatorRejected):
            for extra_note in extract_paired_notes(row.cell_text).notes:
                if extra_note.strip() and extra_note.strip() not in existing_note_texts:
                    existing_note_texts.add(extra_note.strip())
                    units.append(
                        TextUnit(
                            source=UnitSource.BOM_NOTE,
                            location=loc,
                            text=extra_note.strip(),
                            designators=designators,
                            is_xray_marker=False,
                        )
                    )
        if desc_text:
            units.append(
                TextUnit(
                    source=UnitSource.BOM_DESCRIPTION,
                    location=loc,
                    text=desc_text,
                    designators=designators,
                    is_xray_marker=False,
                )
            )

    return Success(units)


# ---- §3.3 Reading the Traveler ---------------------------------------------

def traveler_unit(traveler_wash_text: str | None) -> TextUnit | None:
    """The traveler's wash cell as a unit, read from traveler_metadata (§3.3)."""
    if traveler_wash_text is None:
        return None
    stripped = str(traveler_wash_text).strip()
    if not stripped:
        return None
    return TextUnit(
        source=UnitSource.TRAVELER_WASH,
        location="Traveler wash cell",
        text=stripped,
        designators=(),
        is_xray_marker=False,
    )


# ---- §6.1, §6.2 Fingerprints -----------------------------------------------

def _canonical_pattern_lines(patterns: Sequence[RequirementPattern]) -> list[str]:
    rendered: set[str] = set()
    for p in patterns:
        if not p.groups:
            continue
        sorted_groups = sorted(
            (TermGroup(frozenset(g.alternatives)) for g in p.groups if g.alternatives),
            key=lambda g: tuple(sorted(g.alternatives)),
        )
        if not sorted_groups:
            continue
        win = SINGLE_GROUP_WINDOW if len(sorted_groups) == 1 else p.window_tokens
        canon_pat = RequirementPattern(groups=tuple(sorted_groups), window_tokens=win)
        rendered.add(render_pattern_line(canon_pat))
    return sorted(rendered)


def canonical_vocabulary(vocabulary: RequirementVocabulary) -> dict[str, Any]:
    """The vocabulary's matching content, in one canonical order (§6.1)."""
    kinds_sorted = sorted(RequirementKind, key=lambda k: k.value)
    return {
        "admitted_sources": {
            k.value: sorted({s.value for s in vocabulary.admitted_sources.get(k, frozenset())})
            for k in kinds_sorted
        },
        "negations_after": [
            " ".join(p)
            for p in sorted({tuple(p) for p in vocabulary.negations_after if p})
        ],
        "negations_before": [
            " ".join(p)
            for p in sorted({tuple(p) for p in vocabulary.negations_before if p})
        ],
        "patterns": {
            k.value: _canonical_pattern_lines(vocabulary.patterns.get(k, ()))
            for k in kinds_sorted
        },
        "xray_section_markers": _canonical_pattern_lines(vocabulary.xray_section_markers),
    }


def canonical_json(value: Any) -> str:
    """Canonical JSON: sorted keys, no extra whitespace, non-ASCII escaped (§6.1)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def scan_fingerprint(inputs: ScanInputs) -> Fingerprint:
    """Identifies the inputs a scan was computed from (§6.1)."""
    payload = {
        "bom_hash": inputs.bom_hash,
        "cleaning_rules_version": CLEANING_RULES_VERSION,
        "extractor_version": EXTRACTOR_VERSION,
        "notes_hash": inputs.notes_hash,
        "traveler_wash_text": inputs.traveler_wash_text,
        "vocabulary": canonical_vocabulary(inputs.vocabulary),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def xray_task_fingerprint(
    matches: Sequence[RequirementMatch],
    eco_locations: set[RefDes],
) -> Fingerprint | None:
    """Identifies the X-ray work a tick is made against (§6.2)."""
    has_save_images = any(
        m.kind == RequirementKind.XRAY_SAVE_IMAGES and not m.is_negated
        for m in matches
    )
    if not has_save_images:
        return None

    instructions: set[str] = set()
    locations: set[str] = set(eco_locations)

    for m in matches:
        if m.is_negated:
            continue
        if (
            m.unit.source == UnitSource.ECO_TEXT
            and not m.unit.is_xray_marker
            and m.kind in (RequirementKind.XRAY_SAVE_IMAGES, RequirementKind.XRAY)
        ):
            norm_unit_text = " ".join(t.text for t in normalise(m.unit.text))
            if norm_unit_text:
                instructions.add(norm_unit_text)
        elif m.unit.source == UnitSource.BOM_NOTE:
            if m.kind == RequirementKind.XRAY_SAVE_IMAGES:
                norm_unit_text = " ".join(t.text for t in normalise(m.unit.text))
                if norm_unit_text:
                    instructions.add(norm_unit_text)
            elif m.kind == RequirementKind.XRAY:
                locations.update(m.unit.designators)

    payload = {
        "instructions": sorted(instructions),
        "locations": sorted(locations),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---- §4.6 Deciding ---------------------------------------------------------

def _cut_excerpt(text: str, match_start: int, match_end: int) -> str:
    clean = " ".join(text.split())
    if len(text) <= MAX_EXCERPT_CHARS and len(clean) <= MAX_EXCERPT_CHARS:
        return clean

    # Slice around [match_start, match_end] in original text, then collapse whitespace.
    span_len = max(1, match_end - match_start)
    budget = MAX_EXCERPT_CHARS - 2  # room for possible leading/trailing ellipsis
    if span_len >= budget:
        raw_slice = text[match_start : match_start + budget]
        prefix = "…" if match_start > 0 else ""
        suffix = "…" if match_start + budget < len(text) else ""
        res = f"{prefix}{' '.join(raw_slice.split())}{suffix}"
        return res[:MAX_EXCERPT_CHARS]

    pad = (budget - span_len) // 2
    left = max(0, match_start - pad)
    right = min(len(text), left + budget)
    left = max(0, right - budget)
    prefix = "…" if left > 0 else ""
    suffix = "…" if right < len(text) else ""
    res = f"{prefix}{' '.join(text[left:right].split())}{suffix}"
    return res[:MAX_EXCERPT_CHARS]


def _match_unit_for_kind(
    unit: TextUnit,
    tokens: Sequence[Token],
    kind: RequirementKind,
    vocabulary: RequirementVocabulary,
) -> list[RequirementMatch]:
    if unit.source not in vocabulary.admitted_sources.get(kind, frozenset()):
        return []
    if kind == RequirementKind.XRAY and unit.is_xray_marker:
        return []

    patterns = vocabulary.patterns.get(kind, ())
    if not patterns or not tokens:
        return []

    raw_spans: list[tuple[int, int, int]] = []
    for pat in patterns:
        for first, last in find_matches(tokens, pat, vocabulary):
            raw_spans.append((last - first, first, last))

    if not raw_spans:
        return []

    # De-duplicate / resolve overlaps across patterns of the same kind.
    raw_spans.sort()
    chosen_spans: list[tuple[int, int]] = []
    for _spread, first, last in raw_spans:
        if any(first <= c_last and c_first <= last for c_first, c_last in chosen_spans):
            continue
        chosen_spans.append((first, last))
    chosen_spans.sort()

    matches: list[RequirementMatch] = []
    for first, last in chosen_spans:
        neg = is_negated(tokens, first, last, vocabulary)
        span_toks = tuple(tokens[i].text for i in range(first, last + 1))
        matches.append(
            RequirementMatch(
                kind=kind,
                unit=unit,
                first_token=first,
                last_token=last,
                span_tokens=span_toks,
                is_negated=neg,
            )
        )
    return matches


def decide_requirements(
    eco: Result[EcoUnits, DocumentUnreadable],
    bom: Result[list[TextUnit], DocumentUnreadable],
    traveler: TextUnit | None,
    inputs: ScanInputs,
) -> RequirementScan:
    """Decides every requirement for one audit (§4.6)."""
    vocab = inputs.vocabulary
    all_matches: list[RequirementMatch] = []
    all_evidence: list[RequirementEvidence] = []

    def _scan_unit(unit: TextUnit) -> None:
        toks = normalise(unit.text)
        if not toks:
            return
        for kind in (
            RequirementKind.XRAY_SAVE_IMAGES,
            RequirementKind.XRAY,
            RequirementKind.PRESS_FIT,
            RequirementKind.AQUANOX,
        ):
            unit_matches = _match_unit_for_kind(unit, toks, kind, vocab)
            for m in unit_matches:
                all_matches.append(m)
                excerpt = _cut_excerpt(
                    unit.text,
                    toks[m.first_token].start,
                    toks[m.last_token].end,
                )
                all_evidence.append(
                    RequirementEvidence(
                        kind=m.kind,
                        source=unit.source,
                        location=unit.location,
                        excerpt=excerpt,
                        is_negated=m.is_negated,
                    )
                )

    eco_locations: set[RefDes] = set()
    if eco.is_ok:
        eco_val = eco.value
        eco_locations = set(eco_val.xray_locations)
        for u in eco_val.units:
            _scan_unit(u)
        if isinstance(eco_val.xray_section, TableRows) and eco_val.xray_section.live_rows > 0:
            n_rows = eco_val.xray_section.live_rows
            locs_str = ", ".join(sorted(eco_locations))
            table_excerpt = (
                f"{n_rows} X-ray table row(s): {locs_str}"
                if locs_str
                else f"{n_rows} X-ray table row(s)"
            )[:MAX_EXCERPT_CHARS]
            all_evidence.append(
                RequirementEvidence(
                    kind=RequirementKind.XRAY,
                    source=UnitSource.ECO_TEXT,
                    location="ECO X-ray table",
                    excerpt=table_excerpt,
                    is_negated=False,
                )
            )

    if bom.is_ok:
        for u in bom.value:
            _scan_unit(u)

    if traveler is not None:
        _scan_unit(traveler)

    states: dict[RequirementKind, RequirementState] = {}
    unknown_reasons: dict[RequirementKind, str] = {}

    for kind in (
        RequirementKind.XRAY_SAVE_IMAGES,
        RequirementKind.XRAY,
        RequirementKind.PRESS_FIT,
        RequirementKind.AQUANOX,
    ):
        admitted = vocab.admitted_sources.get(kind, frozenset())
        has_positive_match = any(
            m.kind == kind and not m.is_negated for m in all_matches
        )
        has_xray_table = (
            kind == RequirementKind.XRAY
            and eco.is_ok
            and isinstance(eco.value.xray_section, TableRows)
            and eco.value.xray_section.live_rows > 0
        )

        if has_positive_match or has_xray_table:
            states[kind] = RequirementState.DETECTED
            continue

        reasons: list[str] = []
        needs_eco = (UnitSource.ECO_TEXT in admitted) or (kind == RequirementKind.XRAY)
        needs_bom = (
            UnitSource.BOM_NOTE in admitted
            or UnitSource.BOM_DESCRIPTION in admitted
        )

        if needs_eco and eco.is_err:
            reasons.append(f"ECO unreadable: {eco.error.reason}")
        elif (
            kind == RequirementKind.XRAY
            and eco.is_ok
            and isinstance(eco.value.xray_section, Unrecognised)
        ):
            reasons.append(
                f"X-ray section not recognised: followed by {eco.value.xray_section.following}"
            )

        if needs_bom and bom.is_err:
            reasons.append(f"BOM unreadable: {bom.error.reason}")

        if reasons:
            states[kind] = RequirementState.UNKNOWN
            unknown_reasons[kind] = "; ".join(reasons)
        else:
            states[kind] = RequirementState.ABSENT

    xray_task: Fingerprint | None = None
    if states[RequirementKind.XRAY_SAVE_IMAGES] == RequirementState.DETECTED:
        xray_task = xray_task_fingerprint(all_matches, eco_locations)

    return RequirementScan(
        states=states,
        evidence=all_evidence[:MAX_EVIDENCE],
        unknown_reasons=unknown_reasons,
        xray_task=xray_task,
        fingerprint=scan_fingerprint(inputs),
    )


def scan_documents(inputs: ScanInputs) -> RequirementScan:
    """Reads one audit's documents and decides. Never raises (§4.6)."""
    try:
        eco_res = read_eco_units(inputs.notes_path, inputs.vocabulary)
    except Exception as exc:
        eco_res = Failure(DocumentUnreadable(SourceRole.NOTES, type(exc).__name__))

    try:
        bom_res = read_bom_units(inputs.bom_path)
    except Exception as exc:
        bom_res = Failure(DocumentUnreadable(SourceRole.BOM, type(exc).__name__))

    try:
        trav_u = traveler_unit(inputs.traveler_wash_text)
    except Exception:
        trav_u = None

    return decide_requirements(eco_res, bom_res, trav_u, inputs)


# ---- §8 The tick and cell state --------------------------------------------

def xray_cell_state(
    summary: RequirementSummary | None,
    tick: XrayTick,
) -> XrayCellState:
    """What the X-ray cell shows, in the list and in the identity bar (§8)."""
    if summary is None:
        return XrayCellState.NOT_SCANNED
    save_images = summary.states.get(
        RequirementKind.XRAY_SAVE_IMAGES, RequirementState.UNKNOWN
    )
    if save_images == RequirementState.UNKNOWN:
        return XrayCellState.UNKNOWN
    if save_images == RequirementState.DETECTED:
        if not tick.is_done:
            return XrayCellState.PENDING
        if tick.task_at_tick == summary.xray_task:
            return XrayCellState.DONE
        return XrayCellState.DONE_STALE
    xray = summary.states.get(RequirementKind.XRAY, RequirementState.UNKNOWN)
    if xray == RequirementState.DETECTED:
        return XrayCellState.XR_ONLY
    if xray == RequirementState.UNKNOWN:
        return XrayCellState.UNKNOWN
    return XrayCellState.BLANK


def badge_state_of(
    summary: RequirementSummary | None,
    kind: RequirementKind,
) -> BadgeState:
    if summary is None:
        return BadgeState.NOT_SCANNED
    st = summary.states.get(kind, RequirementState.UNKNOWN)
    if st == RequirementState.DETECTED:
        return BadgeState.DETECTED
    if st == RequirementState.ABSENT:
        return BadgeState.ABSENT
    return BadgeState.UNKNOWN


def build_requirement_badges(
    summary: RequirementSummary | None,
    tick: XrayTick,
) -> RequirementBadges:
    return RequirementBadges(
        aquanox=badge_state_of(summary, RequirementKind.AQUANOX),
        press_fit=badge_state_of(summary, RequirementKind.PRESS_FIT),
        xray_cell=xray_cell_state(summary, tick),
    )


# ---- §10.2 Vocabulary serialisation, merge, export & import ----------------

def _pattern_group_set(pattern: RequirementPattern) -> frozenset[frozenset[NormalisedToken]]:
    return frozenset(frozenset(g.alternatives) for g in pattern.groups)


def merge_shipped_patterns(
    stored: RequirementVocabulary,
    stored_generation: int,
    shipped_by_generation: dict[int, RequirementVocabulary] | None = None,
) -> PatternMergeOutcome:
    """Merges patterns shipped after your stored generation (§10.2)."""
    if shipped_by_generation is None:
        shipped_by_generation = SHIPPED_VOCABULARY_BY_GENERATION

    merged_patterns: dict[RequirementKind, list[RequirementPattern]] = {
        kind: list(stored.patterns.get(kind, ())) for kind in RequirementKind
    }
    present_group_sets: dict[RequirementKind, set[frozenset[frozenset[NormalisedToken]]]] = {
        kind: {_pattern_group_set(p) for p in merged_patterns[kind]}
        for kind in RequirementKind
    }

    merged_markers: list[RequirementPattern] = list(stored.xray_section_markers)
    present_marker_sets: set[frozenset[frozenset[NormalisedToken]]] = {
        _pattern_group_set(p) for p in merged_markers
    }

    merged_neg_before: list[tuple[NormalisedToken, ...]] = list(stored.negations_before)
    present_neg_before: set[tuple[NormalisedToken, ...]] = set(merged_neg_before)

    merged_neg_after: list[tuple[NormalisedToken, ...]] = list(stored.negations_after)
    present_neg_after: set[tuple[NormalisedToken, ...]] = set(merged_neg_after)

    added: list[AddedPattern] = []

    for gen in sorted(shipped_by_generation):
        if gen <= stored_generation:
            old_shipped = shipped_by_generation[gen]
            for kind in RequirementKind:
                for pat in old_shipped.patterns.get(kind, ()):
                    present_group_sets[kind].add(_pattern_group_set(pat))
            for pat in old_shipped.xray_section_markers:
                present_marker_sets.add(_pattern_group_set(pat))
            for phrase in old_shipped.negations_before:
                present_neg_before.add(phrase)
            for phrase in old_shipped.negations_after:
                present_neg_after.add(phrase)
            continue
        shipped = shipped_by_generation[gen]
        for kind in RequirementKind:
            for pat in shipped.patterns.get(kind, ()):
                gset = _pattern_group_set(pat)
                if gset in present_group_sets[kind]:
                    continue
                present_group_sets[kind].add(gset)
                merged_patterns[kind].append(pat)
                added.append(
                    AddedPattern(
                        kind=kind,
                        box=VocabularyBox.PATTERNS,
                        line=render_pattern_line(pat),
                    )
                )
        for pat in shipped.xray_section_markers:
            gset = _pattern_group_set(pat)
            if gset in present_marker_sets:
                continue
            present_marker_sets.add(gset)
            merged_markers.append(pat)
            added.append(
                AddedPattern(
                    kind=None,
                    box=VocabularyBox.SECTION_MARKERS,
                    line=render_pattern_line(pat),
                )
            )
        for phrase in shipped.negations_before:
            if phrase in present_neg_before:
                continue
            present_neg_before.add(phrase)
            merged_neg_before.append(phrase)
            added.append(
                AddedPattern(
                    kind=None,
                    box=VocabularyBox.NEGATIONS_BEFORE,
                    line=" ".join(phrase),
                )
            )
        for phrase in shipped.negations_after:
            if phrase in present_neg_after:
                continue
            present_neg_after.add(phrase)
            merged_neg_after.append(phrase)
            added.append(
                AddedPattern(
                    kind=None,
                    box=VocabularyBox.NEGATIONS_AFTER,
                    line=" ".join(phrase),
                )
            )

    max_shipped = max(shipped_by_generation.keys(), default=DEFAULT_VOCABULARY_GENERATION)
    target_gen = max(stored_generation, DEFAULT_VOCABULARY_GENERATION, max_shipped)

    new_vocab = RequirementVocabulary(
        patterns={k: tuple(v) for k, v in merged_patterns.items()},
        xray_section_markers=tuple(merged_markers),
        admitted_sources=dict(stored.admitted_sources),
        negations_before=tuple(merged_neg_before),
        negations_after=tuple(merged_neg_after),
        generation=target_gen,
    )
    return PatternMergeOutcome(
        vocabulary=new_vocab,
        added=added,
        generation_advanced_to=target_gen,
    )


def vocabulary_to_storage_dict(vocabulary: RequirementVocabulary) -> dict[str, Any]:
    """Serialises a RequirementVocabulary into a JSON-compatible dictionary."""
    kinds_sorted = sorted(RequirementKind, key=lambda k: k.value)
    return {
        "admitted_sources": {
            k.value: sorted(s.value for s in vocabulary.admitted_sources.get(k, frozenset()))
            for k in kinds_sorted
        },
        "generation": int(vocabulary.generation),
        "negations_after": [" ".join(p) for p in vocabulary.negations_after],
        "negations_before": [" ".join(p) for p in vocabulary.negations_before],
        "patterns": {
            k.value: [render_pattern_line(p) for p in vocabulary.patterns.get(k, ())]
            for k in kinds_sorted
        },
        "xray_section_markers": [
            render_pattern_line(p) for p in vocabulary.xray_section_markers
        ],
    }


def vocabulary_to_export_dict(vocabulary: RequirementVocabulary) -> dict[str, Any]:
    """Builds the export payload (§10.2)."""
    data = vocabulary_to_storage_dict(vocabulary)
    data["format"] = VOCABULARY_EXPORT_FORMAT
    data["version"] = VOCABULARY_EXPORT_VERSION
    return data


def parse_vocabulary_dict(
    data: Any,
    *,
    require_export_header: bool = False,
) -> Result[RequirementVocabulary, str]:
    """Validates and parses a vocabulary dictionary from QSettings or an import file."""
    if not isinstance(data, dict):
        return Failure("Vocabulary payload must be a JSON object")

    if require_export_header:
        fmt = data.get("format")
        if fmt != VOCABULARY_EXPORT_FORMAT:
            return Failure(f"Unrecognised vocabulary format: {fmt!r}")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int):
            return Failure("Invalid format version")
        if version != VOCABULARY_EXPORT_VERSION:
            return Failure("made by a newer Cockpit; update this PC first")

    gen = data.get("generation", DEFAULT_VOCABULARY_GENERATION)
    if isinstance(gen, bool) or not isinstance(gen, int) or gen < 1:
        return Failure("Invalid vocabulary generation")

    known_kinds = {k.value: k for k in RequirementKind}
    known_sources = {s.value: s for s in UnitSource}

    raw_patterns = data.get("patterns")
    if not isinstance(raw_patterns, dict):
        return Failure("Missing or invalid 'patterns' map")
    for k_str in raw_patterns:
        if k_str not in known_kinds:
            return Failure("made by a newer Cockpit; update this PC first")

    raw_sources = data.get("admitted_sources")
    if not isinstance(raw_sources, dict):
        return Failure("Missing or invalid 'admitted_sources' map")
    for k_str in raw_sources:
        if k_str not in known_kinds:
            return Failure("made by a newer Cockpit; update this PC first")

    parsed_patterns: dict[RequirementKind, tuple[RequirementPattern, ...]] = {}
    for k_str, kind in known_kinds.items():
        lines = raw_patterns.get(k_str, [])
        if not isinstance(lines, list):
            return Failure(f"Patterns for '{k_str}' must be a list")
        if len(lines) > MAX_PATTERNS_PER_KIND:
            return Failure(
                f"Too many patterns for '{k_str}' (max {MAX_PATTERNS_PER_KIND})"
            )
        pats: list[RequirementPattern] = []
        for idx, line in enumerate(lines, 1):
            if not isinstance(line, str):
                return Failure(f"Pattern {idx} in '{k_str}' must be a string")
            res = parse_pattern_line(line)
            if res.is_err:
                return Failure(f"Invalid pattern in '{k_str}' line {idx}: {res.error.reason}")
            pats.append(res.value)
        parsed_patterns[kind] = tuple(pats)

    raw_markers = data.get("xray_section_markers", [])
    if not isinstance(raw_markers, list):
        return Failure("'xray_section_markers' must be a list")
    if len(raw_markers) > MAX_PATTERNS_PER_KIND:
        return Failure(f"Too many section markers (max {MAX_PATTERNS_PER_KIND})")
    markers: list[RequirementPattern] = []
    for idx, line in enumerate(raw_markers, 1):
        if not isinstance(line, str):
            return Failure(f"Section marker {idx} must be a string")
        res = parse_pattern_line(line)
        if res.is_err:
            return Failure(f"Invalid section marker line {idx}: {res.error.reason}")
        markers.append(res.value)

    parsed_sources: dict[RequirementKind, frozenset[UnitSource]] = {}
    for k_str, kind in known_kinds.items():
        src_list = raw_sources.get(k_str, [])
        if not isinstance(src_list, list):
            return Failure(f"Admitted sources for '{k_str}' must be a list")
        srcs: set[UnitSource] = set()
        for s_str in src_list:
            if not isinstance(s_str, str) or s_str not in known_sources:
                return Failure(f"Unknown source {s_str!r} in '{k_str}'")
            srcs.append(known_sources[s_str]) if isinstance(srcs, list) else srcs.add(known_sources[s_str])
        parsed_sources[kind] = frozenset(srcs)

    def _parse_neg_list(field_name: str) -> Result[tuple[tuple[NormalisedToken, ...], ...], str]:
        raw_list = data.get(field_name, [])
        if not isinstance(raw_list, list):
            return Failure(f"'{field_name}' must be a list")
        if len(raw_list) > MAX_PATTERNS_PER_KIND:
            return Failure(f"Too many phrases in '{field_name}' (max {MAX_PATTERNS_PER_KIND})")
        phrases: list[tuple[NormalisedToken, ...]] = []
        for idx, item in enumerate(raw_list, 1):
            if isinstance(item, str):
                res = parse_negation_line(item)
                if res.is_err:
                    return Failure(f"Invalid phrase in '{field_name}' line {idx}: {res.error.reason}")
                phrases.append(res.value)
            elif isinstance(item, list) and all(isinstance(w, str) for w in item):
                res = parse_negation_line(" ".join(item))
                if res.is_err:
                    return Failure(f"Invalid phrase in '{field_name}' line {idx}: {res.error.reason}")
                phrases.append(res.value)
            else:
                return Failure(f"Invalid phrase in '{field_name}' line {idx}")
        return Success(tuple(phrases))

    neg_before_res = _parse_neg_list("negations_before")
    if neg_before_res.is_err:
        return Failure(neg_before_res.error)
    neg_after_res = _parse_neg_list("negations_after")
    if neg_after_res.is_err:
        return Failure(neg_after_res.error)

    return Success(
        RequirementVocabulary(
            patterns=parsed_patterns,
            xray_section_markers=tuple(markers),
            admitted_sources=parsed_sources,
            negations_before=neg_before_res.value,
            negations_after=neg_after_res.value,
            generation=gen,
        )
    )


def import_vocabulary_file(path: Path) -> Result[RequirementVocabulary, str]:
    """Reads and validates an exported vocabulary file (§10.2, §13)."""
    try:
        path = Path(path)
        if not path.is_file():
            return Failure("File does not exist")
        size = path.stat().st_size
        if size > MAX_VOCABULARY_FILE_BYTES:
            return Failure(
                f"File exceeds maximum size of {MAX_VOCABULARY_FILE_BYTES} bytes"
            )
        raw_bytes = path.read_bytes()
        if len(raw_bytes) > MAX_VOCABULARY_FILE_BYTES:
            return Failure(
                f"File exceeds maximum size of {MAX_VOCABULARY_FILE_BYTES} bytes"
            )
        data = json.loads(raw_bytes.decode("utf-8"))
    except UnicodeDecodeError:
        return Failure("File is not valid UTF-8 text")
    except json.JSONDecodeError as exc:
        return Failure(f"Invalid JSON: {exc.msg}")
    except OSError as exc:
        return Failure(f"Could not read file: {exc}")

    return parse_vocabulary_dict(data, require_export_header=True)


def export_vocabulary_file(path: Path, vocabulary: RequirementVocabulary) -> None:
    """Writes a vocabulary to disk in the export format (§10.2)."""
    payload = vocabulary_to_export_dict(vocabulary)
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    Path(path).write_text(text + "\n", encoding="utf-8")


def save_vocabulary_to_settings(
    settings: Any,
    vocabulary: RequirementVocabulary,
) -> None:
    """Persists vocabulary and generation to QSettings in canonical JSON (§6.1, §10.1)."""
    raw_json = canonical_json(vocabulary_to_storage_dict(vocabulary))
    settings.setValue(VOCABULARY_SETTINGS_KEY, raw_json)
    settings.setValue(VOCABULARY_GENERATION_KEY, int(vocabulary.generation))
    if hasattr(settings, "sync"):
        settings.sync()


def load_vocabulary(
    settings: Any,
    shipped_by_generation: dict[int, RequirementVocabulary] | None = None,
) -> PatternMergeOutcome:
    """The vocabulary in force on this PC (§10.2)."""
    if settings is None or not settings.contains(VOCABULARY_SETTINGS_KEY):
        return PatternMergeOutcome(
            vocabulary=DEFAULT_VOCABULARY,
            added=[],
            generation_advanced_to=DEFAULT_VOCABULARY_GENERATION,
        )

    raw_val = settings.value(VOCABULARY_SETTINGS_KEY)
    if not isinstance(raw_val, str) or not raw_val.strip():
        logger.warning("Malformed requirements/vocabulary in QSettings; using defaults")
        return PatternMergeOutcome(
            vocabulary=DEFAULT_VOCABULARY,
            added=[],
            generation_advanced_to=DEFAULT_VOCABULARY_GENERATION,
        )

    try:
        data = json.loads(raw_val)
    except json.JSONDecodeError:
        logger.warning("Corrupt JSON in requirements/vocabulary; using defaults")
        return PatternMergeOutcome(
            vocabulary=DEFAULT_VOCABULARY,
            added=[],
            generation_advanced_to=DEFAULT_VOCABULARY_GENERATION,
        )

    parsed_res = parse_vocabulary_dict(data, require_export_header=False)
    if parsed_res.is_err:
        logger.warning("Invalid stored vocabulary (%s); using defaults", parsed_res.error)
        return PatternMergeOutcome(
            vocabulary=DEFAULT_VOCABULARY,
            added=[],
            generation_advanced_to=DEFAULT_VOCABULARY_GENERATION,
        )

    stored_vocab = parsed_res.value
    stored_gen = stored_vocab.generation
    if settings.contains(VOCABULARY_GENERATION_KEY):
        try:
            stored_gen = int(settings.value(VOCABULARY_GENERATION_KEY))
        except (TypeError, ValueError):
            stored_gen = stored_vocab.generation

    outcome = merge_shipped_patterns(
        stored_vocab,
        stored_gen,
        shipped_by_generation=shipped_by_generation,
    )

    if outcome.added or outcome.generation_advanced_to != stored_gen:
        try:
            save_vocabulary_to_settings(settings, outcome.vocabulary)
        except Exception:
            logger.exception("Could not write merged process requirements vocabulary")

    return outcome


class EvidenceCache:
    """Bounded LRU cache of evidence lists keyed by (audit_id, fingerprint) (§9)."""

    def __init__(self, max_entries: int = EVIDENCE_CACHE_ENTRIES) -> None:
        self._max_entries = max_entries
        self._store: OrderedDict[tuple[int, str], list[RequirementEvidence]] = OrderedDict()

    def get(
        self,
        audit_id: int,
        fingerprint: str,
        loader: Callable[[int], list[RequirementEvidence]],
    ) -> list[RequirementEvidence]:
        key = (audit_id, fingerprint)
        if key in self._store:
            self._store.move_to_end(key)
            return self._store[key]
        items = list(loader(audit_id))
        self._store[key] = items
        if len(self._store) > self._max_entries:
            self._store.popitem(last=False)
        return items

    def clear(self) -> None:
        self._store.clear()


_SOURCE_LABELS: dict[UnitSource, str] = {
    UnitSource.ECO_TEXT: "ECO",
    UnitSource.BOM_NOTE: "BOM note",
    UnitSource.BOM_DESCRIPTION: "BOM description",
    UnitSource.TRAVELER_WASH: "Traveler wash",
}


def format_requirement_tooltip(
    *,
    kinds: Sequence[RequirementKind],
    summary: RequirementSummary | None,
    tick: XrayTick = XrayTick(),
    evidence: Sequence[RequirementEvidence] = (),
    is_xray_column: bool = False,
    is_stale_scan: bool = False,
) -> str | None:
    """Formats an HTML-escaped tooltip for AQ, PF, or X-ray in the list or identity bar (§9)."""
    if summary is None:
        return "<span>Not scanned yet; Cockpit reads it shortly</span>"

    lines: list[str] = []
    kind_set = set(kinds)

    if is_xray_column:
        cell_st = xray_cell_state(summary, tick)
        if cell_st == XrayCellState.DONE_STALE:
            date_str = (
                tick.ticked_at.strftime("%Y-%m-%d")
                if tick.ticked_at is not None
                else "earlier date"
            )
            lines.append(
                html.escape(
                    f"Ticked {date_str}; the X-ray requirements changed since"
                )
            )

    for k in kinds:
        if summary.states.get(k) == RequirementState.UNKNOWN:
            reason = summary.unknown_reasons.get(k)
            if reason:
                lines.append(html.escape(reason))

    pos_ev = [e for e in evidence if e.kind in kind_set and not e.is_negated]
    neg_ev = [e for e in evidence if e.kind in kind_set and e.is_negated]

    for ev in pos_ev:
        src_lbl = _SOURCE_LABELS.get(ev.source, ev.source.value)
        loc_esc = html.escape(ev.location)
        exc_esc = html.escape(ev.excerpt)
        lines.append(f"{html.escape(src_lbl)} ({loc_esc}): {exc_esc}")

    for ev in neg_ev:
        src_lbl = _SOURCE_LABELS.get(ev.source, ev.source.value)
        loc_esc = html.escape(ev.location)
        exc_esc = html.escape(ev.excerpt)
        lines.append(
            f"Ignored (negated): {html.escape(src_lbl)} ({loc_esc}): {exc_esc}"
        )

    if is_stale_scan:
        lines.append(html.escape("Rescan pending"))

    if not lines:
        return None
    return "<span>" + "<br/>".join(lines) + "</span>"
