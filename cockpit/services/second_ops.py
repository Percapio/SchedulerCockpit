from cockpit.utils.sorting import natural_sort_key
import re
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal, QSettings

from ..ingestion.parsers.audit_bom import RawBomRow, read_raw_rows
from ..ingestion.errors import MalformedBomError
from ..persistence.repositories.bom_components import AuditBomComponentRepository
from ..persistence.repositories.source_files import SourceFileRepository

# Shipped vocabulary, versioned. A merge that ran unconditionally would re-add
# terms an operator deliberately deleted, so each store records the highest
# generation already merged into it and only later generations are offered.
#
# TERM BLOCK PLUG is deliberately absent from generation 2: matches_any_term is
# a token-subsequence test, so PLUG and BLOCK each independently match
# everything the compound entry would. It is unreachable.
SHIPPED_TERMS_BY_GENERATION: dict[int, tuple[str, ...]] = {
    1: ("Fuse", "Shunt", "SHNT", "JMPER", "JUMPER", "Screw", "Nut"),
    2: (
        "WSH", "WASHER", "HEATSINK", "PLUG", "BLOCK", "NYLON", "COVER",
        "SHIELD", "SHLD", "M80-", "M83-", "E222YL", "LABEL", "LABL",
        "PLASTIC", "DOWSIL", "RTV", "DWSIL", "LOCTITE",
    ),
}

DEFAULT_TERMS_GENERATION: int = max(SHIPPED_TERMS_BY_GENERATION)

# Derived from the generation map rather than written out a second time: two
# hand-maintained copies of one vocabulary would drift invisibly, shipping one
# list as the default while the merge delivered another.
DEFAULT_SECOND_OPS_TERMS: tuple[str, ...] = tuple(
    term
    for generation in sorted(SHIPPED_TERMS_BY_GENERATION)
    for term in SHIPPED_TERMS_BY_GENERATION[generation]
)

SECOND_OPS_TERMS_KEY: str = "second_ops/terms"
SECOND_OPS_TERMS_GENERATION_KEY: str = "second_ops/terms_generation"


@dataclass(frozen=True)
class TermMergeOutcome:
    """Result of one migration run, for the operator-facing notice."""

    added: tuple[str, ...]
    generation_advanced_to: int


def merge_shipped_terms(
    stored_terms: tuple[str, ...],
    stored_generation: int,
) -> tuple[str, ...]:
    """Merges shipped terms introduced after the store was last updated.

    pre:  stored_generation <= DEFAULT_TERMS_GENERATION
    post: every term from a generation above stored_generation that is not
          already present, case-insensitively, is appended in shipped order;
          terms present at any casing keep their existing spelling; terms the
          operator deleted at or below stored_generation stay deleted; the
          operator's own terms keep their relative order and precede the
          appended ones
    """
    present = {term.lower() for term in stored_terms}
    merged = list(stored_terms)

    for generation in sorted(SHIPPED_TERMS_BY_GENERATION):
        if generation <= stored_generation:
            continue
        for term in SHIPPED_TERMS_BY_GENERATION[generation]:
            if term.lower() in present:
                continue
            present.add(term.lower())
            merged.append(term)

    return tuple(merged)


class SecondOpsSettingsController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

    def terms(self) -> tuple[str, ...]:
        if not self._settings.contains(SECOND_OPS_TERMS_KEY):
            return DEFAULT_SECOND_OPS_TERMS
            
        val = self._settings.value(SECOND_OPS_TERMS_KEY)
        if val is None:
            return ()
        
        if not isinstance(val, str):
            import logging
            logging.getLogger(__name__).warning("Malformed 2nd OPS terms in QSettings")
            return DEFAULT_SECOND_OPS_TERMS
            
        if not val.strip():
            return ()
            
        return tuple(part.strip() for part in val.split(",") if part.strip())

    def set_terms_from_text(self, raw_text: str) -> None:
        parts = [p.strip() for p in raw_text.split(",") if p.strip()]
        
        seen = set()
        normalized = []
        for p in parts:
            p_lower = p.lower()
            if p_lower not in seen:
                seen.add(p_lower)
                normalized.append(p)
                
        new_val = ", ".join(normalized) if normalized else ""

        if self._settings.contains(SECOND_OPS_TERMS_KEY) and self._stamped_generation_is_current():
            current_val = self._settings.value(SECOND_OPS_TERMS_KEY)
            # handle cases where current_val might be null if QSettings is weird
            if current_val is None:
                current_val = ""
            if isinstance(current_val, str) and current_val == new_val:
                return

        self._settings.setValue(SECOND_OPS_TERMS_KEY, new_val)
        # Stamped with the terms, never separately. A store that gained a terms
        # key without a generation would be read as generation 1 on the next
        # launch and have every later term re-appended — including the ones the
        # operator had just deleted.
        self._settings.setValue(SECOND_OPS_TERMS_GENERATION_KEY, DEFAULT_TERMS_GENERATION)
        self.changed.emit()

    def _stamped_generation_is_current(self) -> bool:
        return self.stored_generation() == DEFAULT_TERMS_GENERATION

    def stored_generation(self) -> int:
        """The highest shipped generation already merged into this store.

        A store holding terms but no marker predates the marker and has, by
        definition, seen generation 1.
        """
        if not self._settings.contains(SECOND_OPS_TERMS_GENERATION_KEY):
            return 1
        try:
            return int(self._settings.value(SECOND_OPS_TERMS_GENERATION_KEY))
        except (TypeError, ValueError):
            return 1

    def migrate_shipped_terms(self) -> TermMergeOutcome:
        """Brings a stored vocabulary up to the shipped generation, once.

        pre:  called once per process, before any consumer reads terms()
        post: a store with no terms key is left untouched and added is empty,
              because terms() already resolves to the current defaults;
              otherwise the merged list and the new generation are persisted
              together and changed is emitted once
        """
        if not self._settings.contains(SECOND_OPS_TERMS_KEY):
            return TermMergeOutcome(added=(), generation_advanced_to=DEFAULT_TERMS_GENERATION)

        stored_generation = self.stored_generation()
        if stored_generation >= DEFAULT_TERMS_GENERATION:
            return TermMergeOutcome(added=(), generation_advanced_to=stored_generation)

        stored_terms = self.terms()
        merged = merge_shipped_terms(stored_terms, stored_generation)
        added = merged[len(stored_terms):]

        try:
            self._settings.setValue(SECOND_OPS_TERMS_KEY, ", ".join(merged))
            self._settings.setValue(
                SECOND_OPS_TERMS_GENERATION_KEY, DEFAULT_TERMS_GENERATION
            )
            self._settings.sync()
        except Exception:
            # A vocabulary update must never fail startup. The store keeps a
            # coherent older generation and the merge is retried next launch.
            import logging
            logging.getLogger(__name__).exception("2nd OPS term migration could not be written")
            return TermMergeOutcome(added=(), generation_advanced_to=stored_generation)

        if added:
            self.changed.emit()
        return TermMergeOutcome(added=added, generation_advanced_to=DEFAULT_TERMS_GENERATION)

    def restore_defaults(self) -> None:
        had_terms = self._settings.contains(SECOND_OPS_TERMS_KEY)
        had_generation = self._settings.contains(SECOND_OPS_TERMS_GENERATION_KEY)
        if not (had_terms or had_generation):
            return
        # Both keys go together. A generation marker left behind a removed
        # vocabulary would describe a list the store does not have.
        self._settings.remove(SECOND_OPS_TERMS_KEY)
        self._settings.remove(SECOND_OPS_TERMS_GENERATION_KEY)
        self.changed.emit()


def tokenize(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    return tuple(t.lower() for t in re.split(r'[^a-zA-Z0-9]', text) if t)


def matches_any_term(part_number: str, description: str | None, terms: tuple[str, ...]) -> bool:
    if not terms:
        return False
        
    pn_tokens = tokenize(part_number)
    desc_tokens = tokenize(description)
    
    def is_subseq(search_in: tuple[str, ...], to_find: tuple[str, ...]) -> bool:
        if not to_find: return False
        n = len(to_find)
        for i in range(len(search_in) - n + 1):
            if search_in[i:i+n] == to_find:
                return True
        return False

    for term in terms:
        term_tokens = tokenize(term)
        if not term_tokens:
            continue
        if is_subseq(pn_tokens, term_tokens) or is_subseq(desc_tokens, term_tokens):
            return True
            
    return False


from ..persistence.repositories.bom_components import AuditBomComponentRepository, MountCode

def mount_label(code: MountCode) -> str:
    if code == 'T':
        return "THT"
    if code == 'S':
        return "SMT"
    return ""

@dataclass(frozen=True)
class SecondOpsCandidate:
    find_number: str
    component_mpn: str
    description: str | None
    mount_type: MountCode


@dataclass(frozen=True)
class AuditCandidates:
    audit_id: int
    part_number: str
    work_order_ref: str
    candidates: list[SecondOpsCandidate]


@dataclass(frozen=True)
class SecondOpsRow:
    row: RawBomRow
    is_match: bool


class ReadFailureCause(Enum):
    NO_BOM_SOURCE_FILE = auto()
    FILE_MISSING = auto()
    UNREADABLE = auto()


def list_candidates_for_open_audits(
    bom_component_repo: AuditBomComponentRepository,
    terms: tuple[str, ...]
) -> list[AuditCandidates]:
    if not terms:
        return []

    lines = bom_component_repo.list_bom_lines_for_all_active_audits()
    
    grouped = {}
    for line in lines:
        if line.audit_id not in grouped:
            display_part = line.part_number
            if line.split_suffix:
                display_part += line.split_suffix
                
            grouped[line.audit_id] = {
                "part_number": display_part,
                "work_order_ref": line.work_order_ref,
                "candidates": []
            }
            
        if matches_any_term(line.component_mpn, line.description, terms):
            grouped[line.audit_id]["candidates"].append(
                SecondOpsCandidate(
                    find_number=line.find_number,
                    component_mpn=line.component_mpn,
                    description=line.description,
                    mount_type=line.mount_type
                )
            )

    result = []
    for audit_id, data in grouped.items():
        if data["candidates"]:
            data["candidates"].sort(key=lambda c: natural_sort_key(c.find_number))
            result.append(AuditCandidates(
                audit_id=audit_id,
                part_number=data["part_number"],
                work_order_ref=data["work_order_ref"],
                candidates=data["candidates"]
            ))
            
    return result


def resolve_bom_workbook(
    audit_id: int,
    source_file_repo: SourceFileRepository
) -> Path | ReadFailureCause:
    files = source_file_repo.list_for_audit(audit_id)
    bom_files = [f for f in files if f.file_category == "BOM"]
    if not bom_files:
        return ReadFailureCause.NO_BOM_SOURCE_FILE
        
    path = bom_files[0].local_storage_path
    if not path.exists():
        return ReadFailureCause.FILE_MISSING
        
    return path


def read_second_ops_rows(
    workbook_path: Path,
    terms: tuple[str, ...]
) -> list[SecondOpsRow] | ReadFailureCause:
    try:
        raw_rows = read_raw_rows(workbook_path)
    except MalformedBomError:
        return ReadFailureCause.UNREADABLE
    except OSError:
        return ReadFailureCause.UNREADABLE
        
    return [
        SecondOpsRow(
            row=row,
            is_match=matches_any_term(row.part_number, row.description, terms)
        )
        for row in raw_rows
    ]


from ..ingestion.parsers.audit_bom import CANONICAL_COLUMNS, SHEET_COLUMN_ORDER

def render_tsv(rows: list[RawBomRow]) -> str:
    lines = []
    for row in rows:
        normalized = []
        for label in SHEET_COLUMN_ORDER:
            idx = CANONICAL_COLUMNS.index(label)
            cell = row.cells[idx]
            if cell is None:
                normalized.append("")
            else:
                norm = re.sub(r'[\t\r\n]+', ' ', str(cell)).strip()
                normalized.append(norm)
        lines.append("\t".join(normalized))
    return "\r\n".join(lines)

