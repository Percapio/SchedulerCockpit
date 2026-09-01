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

DEFAULT_SECOND_OPS_TERMS: tuple[str, ...] = (
    "Fuse", "Shunt", "SHNT", "JMPER", "JUMPER", "Screw", "Nut",
)

SECOND_OPS_TERMS_KEY: str = "second_ops/terms"


class SecondOpsSettingsController(QObject):
    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def terms(self) -> tuple[str, ...]:
        settings = QSettings()
        if not settings.contains(SECOND_OPS_TERMS_KEY):
            return DEFAULT_SECOND_OPS_TERMS
            
        val = settings.value(SECOND_OPS_TERMS_KEY)
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
        
        settings = QSettings()
        if settings.contains(SECOND_OPS_TERMS_KEY):
            current_val = settings.value(SECOND_OPS_TERMS_KEY)
            # handle cases where current_val might be null if QSettings is weird
            if current_val is None:
                current_val = ""
            if isinstance(current_val, str) and current_val == new_val:
                return
                
        settings.setValue(SECOND_OPS_TERMS_KEY, new_val)
        self.changed.emit()

    def restore_defaults(self) -> None:
        settings = QSettings()
        if settings.contains(SECOND_OPS_TERMS_KEY):
            settings.remove(SECOND_OPS_TERMS_KEY)
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


@dataclass(frozen=True)
class SecondOpsCandidate:
    find_number: int
    component_mpn: str
    description: str | None


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
                    description=line.description
                )
            )

    result = []
    for audit_id, data in grouped.items():
        if data["candidates"]:
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


def render_tsv(rows: list[RawBomRow]) -> str:
    lines = []
    for row in rows:
        normalized_cells = []
        for cell in row.cells:
            norm = re.sub(r'[\t\r\n]', ' ', str(cell)).strip()
            normalized_cells.append(norm)
        lines.append("\t".join(normalized_cells))
    return "\r\n".join(lines)
