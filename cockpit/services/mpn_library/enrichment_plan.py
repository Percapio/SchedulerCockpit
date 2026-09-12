from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple, Dict, Optional
from .types import MpnKey
from .schema import LIBRARY_SCHEMA_VERSION

class FailureReason(Enum):
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    SERVER_ERROR = "SERVER_ERROR"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    SNAPSHOT_WRITE_FAILED = "SNAPSHOT_WRITE_FAILED"

class AbortCause(Enum):
    CREDENTIALS = "CREDENTIALS"
    QUOTA = "QUOTA"
    WATCHDOG = "WATCHDOG"
    CANCELLED = "CANCELLED"

@dataclass
class EnrichmentPlan:
    to_query: List[MpnKey] = field(default_factory=list)
    already_known: List[MpnKey] = field(default_factory=list)
    pending_choice: List[MpnKey] = field(default_factory=list)
    suppressed: List[MpnKey] = field(default_factory=list)
    unkeyable: List[str] = field(default_factory=list)
    deferred: List[MpnKey] = field(default_factory=list)

@dataclass
class EnrichmentReport:
    resolved: List[MpnKey] = field(default_factory=list)
    ambiguous: List[MpnKey] = field(default_factory=list)
    not_found: List[MpnKey] = field(default_factory=list)
    failed: List[Tuple[MpnKey, FailureReason]] = field(default_factory=list)
    unqueried: List[MpnKey] = field(default_factory=list)
    unmapped_packaging_labels: List[str] = field(default_factory=list)
    abort_cause: Optional[AbortCause] = None
    requests_spent: int = 0


def plan_enrichment(
    bom_lines: List,
    library_rows: Dict[MpnKey, dict],
    refresh_stale: bool,
    budget: int
) -> EnrichmentPlan:
    """
    Partitions an audit BOM into what will be queried and what will not.
    """
    plan = EnrichmentPlan()
    
    # max queries per part: 1 initial + MAX_RETRIES (3)
    from .digikey_gateway import MAX_RETRIES
    cost_per_part = 1 + MAX_RETRIES
    available_budget = budget - 1 # reserve 1 for token mint
    
    seen_keys = set()
    
    for line in bom_lines:
        from .normalisation import normalise_mpn, Unkeyable
        raw_mpn = line.component_mpn
        
        norm_result = normalise_mpn(raw_mpn)
        if isinstance(norm_result, Unkeyable):
            if raw_mpn not in seen_keys:
                plan.unkeyable.append(raw_mpn)
                seen_keys.add(raw_mpn)
            continue
            
        mpn_key = norm_result
        if mpn_key in seen_keys:
            continue
        seen_keys.add(mpn_key)
        
        row = library_rows.get(mpn_key)
        if not row:
            # Should have been observed by observe_bom, but if not:
            plan.to_query.append(mpn_key)
            continue
            
        status = row.get("resolution_status")
        if status == "UNRESOLVED":
            plan.to_query.append(mpn_key)
        elif status == "AMBIGUOUS":
            plan.pending_choice.append(mpn_key)
        elif status == "NOT_FOUND":
            plan.suppressed.append(mpn_key)
        elif status == "RESOLVED":
            if refresh_stale:
                plan.to_query.append(mpn_key)
            else:
                plan.already_known.append(mpn_key)
                
    # Now partition to_query based on budget
    affordable_count = max(0, available_budget // cost_per_part)
    
    if len(plan.to_query) > affordable_count:
        plan.deferred = plan.to_query[affordable_count:]
        plan.to_query = plan.to_query[:affordable_count]
        
    return plan
