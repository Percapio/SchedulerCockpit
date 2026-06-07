"""Audit read service."""

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.services.views import OpenAuditDigest


from datetime import date, timedelta
import math
from cockpit.services.repeat import derive_repeat

def start_by(ship_date: date | None, total_runtime_hours: float | None, holidays: set[date]) -> date | None:
    if ship_date is None or total_runtime_hours is None:
        return None
        
    days_needed = math.ceil(total_runtime_hours / 8.0)
    if days_needed == 0:
        return ship_date
        
    cursor = ship_date - timedelta(days=1)
    while days_needed > 0:
        if cursor.weekday() < 5 and cursor not in holidays:
            days_needed -= 1
            if days_needed == 0:
                return cursor
        cursor -= timedelta(days=1)
        
    return cursor

class AuditReadService:
    def __init__(self, audit_repo: AuditRepository, holiday_svc=None) -> None:
        self._audit_repo = audit_repo
        self._holiday_svc = holiday_svc

    def list_open(self) -> list[OpenAuditDigest]:
        audits = self._audit_repo.list_open()
        
        holidays = self._holiday_svc.list_holidays() if self._holiday_svc else set()
        
        digests = []
        for a in audits:
            meta = a.traveler_metadata or {}
            
            itar_classification = meta.get("itar_classification", "")
            classification = "ITAR" if str(itar_classification).strip().upper() == "YES" else "Non-ITAR"
            
            ship_date_obj = None
            if a.ship_date:
                try:
                    ship_date_obj = date.fromisoformat(a.ship_date)
                except ValueError:
                    pass
                    
            total_runtime = None
            if a.feeder_setuptime is not None and a.smt_runtime is not None and a.tht_runtime is not None:
                total_runtime = a.feeder_setuptime + a.smt_runtime + a.tht_runtime
                
            start_by_val = start_by(ship_date_obj, total_runtime, holidays)
            
            digests.append(OpenAuditDigest(
                audit_id=a.id,
                part_number=a.part_number,
                work_order_ref=a.work_order_ref,
                split_suffix=a.split_suffix,
                quantity=a.quantity,
                status=a.status,
                updated_at=a.updated_at,
                date_ingested=a.created_at,
                ship_date=ship_date_obj,
                lead_time_days=meta.get("lead_time_days"),
                repeat=derive_repeat(meta),
                classification=classification,
                assembly_class=meta.get("assembly_class"),
                process=meta.get("process"),
                feeder_setuptime=a.feeder_setuptime,
                smt_runtime=a.smt_runtime,
                tht_runtime=a.tht_runtime,
                start_by=start_by_val
            ))
            
        return digests
