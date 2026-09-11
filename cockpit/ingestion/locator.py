"""Job locator."""

import pathlib
import re
from dataclasses import dataclass
from typing import Union

from .roles import role_of, SourceRole
from .errors import (
    JobNumberMalformed,
    SourceRootUnreachable,
    JobDirectoryNotFound,
    JobDirectoryEscapesRoot,
    RequiredRoleMissing,
    JobNumberMismatch,
)
from .categorizer import CategorizedQuartet
from .filename_rules import derive_part_number_from_filename

JOB_NUMBER_GRAMMAR = re.compile(r"^[A-Z][0-9]{6}$")

@dataclass
class CandidateSet:
    role: SourceRole
    candidates: list[pathlib.Path]
    preselected: pathlib.Path | None

@dataclass
class LocatedFiles:
    job_number: str
    job_directory: pathlib.Path
    quartet: CategorizedQuartet
    ignored_count: int

@dataclass
class PendingSelection:
    job_number: str
    job_directory: pathlib.Path
    sets: list[CandidateSet]
    resolved: dict[SourceRole, pathlib.Path]
    ignored_count: int

FetchOutcome = Union[LocatedFiles, PendingSelection]

def resolve_job_directory(job_number: str, source_root: pathlib.Path) -> pathlib.Path:
    first_4 = job_number[:4]
    first_5 = job_number[:5]
    parent_dir = source_root / f"{first_4}xxx" / f"{first_5}xx"
    
    if parent_dir.exists() and parent_dir.is_dir():
        for child in parent_dir.iterdir():
            if child.is_dir() and child.name.startswith(job_number):
                # Ensure it's the exact job number, or followed by a non-alphanumeric char (like space)
                if len(child.name) == len(job_number) or not child.name[len(job_number)].isalnum():
                    return child
                    
    return parent_dir / job_number

def assert_within_root(candidate: pathlib.Path, source_root: pathlib.Path) -> None:
    try:
        resolved_candidate = candidate.resolve()
        resolved_root = source_root.resolve()
    except OSError:
        raise JobDirectoryEscapesRoot(candidate.name, candidate, source_root)
        
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        raise JobDirectoryEscapesRoot(candidate.name, resolved_candidate, resolved_root)

def traveler_revision(file_name: str) -> str | None:
    match = re.search(r"rev\s+([a-zA-Z]+)(?=\s|$|\.|-)", file_name, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    return None

def revision_rank(file_name: str) -> tuple[int, int, str]:
    rev = traveler_revision(file_name)
    if rev is None:
        return (0, 0, "")
    return (1, len(rev), rev)

def locate(job_number: str, source_root: pathlib.Path) -> FetchOutcome:
    # Grammar check before touching share
    job_number_clean = job_number.strip().upper()
    if not JOB_NUMBER_GRAMMAR.match(job_number_clean):
        raise JobNumberMalformed(job_number)
        
    try:
        if not source_root.exists() or not source_root.is_dir():
            raise SourceRootUnreachable(source_root, FileNotFoundError("Root missing or not a directory"))
        # Force a listing to check reachability
        list(source_root.iterdir())
    except Exception as e:
        if isinstance(e, SourceRootUnreachable):
            raise
        raise SourceRootUnreachable(source_root, e)

    job_dir = resolve_job_directory(job_number_clean, source_root)
    if not job_dir.exists():
        raise JobDirectoryNotFound(job_number_clean, job_dir)
        
    assert_within_root(job_dir, source_root)
    
    entries = list(job_dir.iterdir())
    
    candidates_by_role = {
        SourceRole.BOM: [],
        SourceRole.TRAVELER: [],
        SourceRole.NOTES: [],
        SourceRole.PDF: [],
        SourceRole.UNKNOWN: [],
    }
    
    for entry in entries:
        if entry.is_file():
            candidates_by_role[role_of(entry.name)].append(entry)
            
    ignored_count = len(candidates_by_role[SourceRole.UNKNOWN])
    
    resolved = {}
    missing_roles = []
    
    # 1. Missing required roles
    for role, name in [(SourceRole.BOM, "BOM"), (SourceRole.TRAVELER, "TRAVELER"), (SourceRole.NOTES, "NOTES")]:
        if not candidates_by_role[role]:
            missing_roles.append(name)
            
    if missing_roles:
        present = [p.name for p in entries if p.is_file()]
        raise RequiredRoleMissing(job_number_clean, missing_roles, present)

    # 2. Check for ambiguity
    has_ambiguity = False
    pending_sets = []
    
    for role in [SourceRole.BOM, SourceRole.TRAVELER, SourceRole.NOTES, SourceRole.PDF]:
        cands = candidates_by_role[role]
        if role == SourceRole.PDF and not cands:
            continue
            
        if len(cands) == 1:
            resolved[role] = cands[0]
        else:
            has_ambiguity = True
            preselected = None
            if role == SourceRole.TRAVELER:
                ranks = [(c, revision_rank(c.name)) for c in cands]
                top_rank = max(r[1] for r in ranks)
                top_cands = [c for c, r in ranks if r == top_rank]
                if len(top_cands) == 1 and top_rank != (0, 0, ""):
                    preselected = top_cands[0]
                    
            pending_sets.append(CandidateSet(role=role, candidates=cands, preselected=preselected))
            
    if has_ambiguity:
        return PendingSelection(
            job_number=job_number_clean,
            job_directory=job_dir,
            sets=pending_sets,
            resolved=resolved,
            ignored_count=ignored_count
        )
        
    # No ambiguity, verify BOM token
    bom_token = derive_part_number_from_filename(resolved[SourceRole.BOM])
    if bom_token != job_number_clean:
        raise JobNumberMismatch(job_number_clean, resolved[SourceRole.BOM].name, bom_token)
        
    return LocatedFiles(
        job_number=job_number_clean,
        job_directory=job_dir,
        quartet=CategorizedQuartet(
            bom_path=resolved[SourceRole.BOM],
            traveler_path=resolved[SourceRole.TRAVELER],
            notes_path=resolved[SourceRole.NOTES],
            pdf_path=resolved.get(SourceRole.PDF)
        ),
        ignored_count=ignored_count
    )

def resolve_selection(pending: PendingSelection, chosen: dict[SourceRole, pathlib.Path]) -> LocatedFiles:
    resolved = dict(pending.resolved)
    resolved.update(chosen)
    
    bom_token = derive_part_number_from_filename(resolved[SourceRole.BOM])
    if bom_token != pending.job_number:
        raise JobNumberMismatch(pending.job_number, resolved[SourceRole.BOM].name, bom_token)
        
    return LocatedFiles(
        job_number=pending.job_number,
        job_directory=pending.job_directory,
        quartet=CategorizedQuartet(
            bom_path=resolved[SourceRole.BOM],
            traveler_path=resolved[SourceRole.TRAVELER],
            notes_path=resolved[SourceRole.NOTES],
            pdf_path=resolved.get(SourceRole.PDF)
        ),
        ignored_count=pending.ignored_count
    )
