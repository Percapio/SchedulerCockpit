"""Audit read service."""

from datetime import date, timedelta
import json
import logging
import math
import sqlite3

from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.services.company_names import company_key
from cockpit.services.date_urgency import classify_urgency
from cockpit.services.itar import classification_display, is_itar
from cockpit.services.repeat import derive_repeat_marker
from cockpit.services.requirements import HandFlag, RequirementKind, RequirementSummary, XrayTick
from cockpit.services.views import OpenAuditDigest

logger = logging.getLogger(__name__)


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


def _customer_of(meta) -> str:
    """Phase 59 §5.1: the traveler's CUSTOMER cell, trimmed; "" without one."""
    if not isinstance(meta, dict):
        return ""
    return str(meta.get("customer_name") or "").strip()


class AuditReadService:
    def __init__(self, audit_repo: AuditRepository, holiday_svc=None) -> None:
        self._audit_repo = audit_repo
        self._holiday_svc = holiday_svc

    def _map_to_digest(
        self,
        a,
        holidays,
        today_val,
        req_summary: RequirementSummary | None = None,
        hand_flags: dict[RequirementKind, HandFlag] | None = None,
    ) -> OpenAuditDigest:
        meta = a.traveler_metadata or {}

        classification = classification_display(meta)
        is_itar_flag = is_itar(meta)

        ship_date_obj = None
        if a.ship_date:
            try:
                ship_date_obj = date.fromisoformat(a.ship_date)
            except ValueError:
                pass

        total_runtime = None
        if a.feeder_setuptime is not None and a.smt_runtime is not None and a.tht_runtime is not None:
            total_runtime = (
                a.feeder_setuptime + a.smt_runtime + a.tht_runtime
                + (a.aoi_runtime or 0.0)
                + (a.ops_runtime or 0.0)
                + (a.shipping_runtime or 0.0)
            )

        start_by_val = start_by(ship_date_obj, total_runtime, holidays)
        start_by_urgency = classify_urgency(start_by_val, today_val, holidays)
        ship_urgency = classify_urgency(ship_date_obj, today_val, holidays)

        return OpenAuditDigest(
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
            repeat=derive_repeat_marker(meta),
            classification=classification,
            assembly_class=meta.get("assembly_class"),
            process=meta.get("process"),
            feeder_setuptime=a.feeder_setuptime,
            smt_runtime=a.smt_runtime,
            tht_runtime=a.tht_runtime,
            aoi_runtime=a.aoi_runtime,
            ops_runtime=a.ops_runtime,
            shipping_runtime=a.shipping_runtime,
            start_by=start_by_val,
            ops_per_board_min=a.ops_per_board_min,
            is_itar=is_itar_flag,
            process_clean=meta.get("process_clean"),
            article_revision=a.article_revision,
            is_labeled=a.is_labeled,
            are_photos_uploaded=a.are_photos_uploaded,
            start_by_urgency=start_by_urgency,
            ship_urgency=ship_urgency,
            requirements=req_summary,
            xray_tick=XrayTick(
                is_done=bool(getattr(a, "xray_images_done", False)),
                task_at_tick=getattr(a, "xray_images_done_task", None),
                ticked_at=getattr(a, "xray_images_done_at", None),
            ),
            customer=_customer_of(meta),
            hand_flags=dict(hand_flags or {}),
        )

    def _load_summaries(self) -> dict[int, RequirementSummary]:
        req_repo = getattr(self._audit_repo, "requirements_repo", None)
        if req_repo is None:
            return {}
        try:
            return req_repo.find_summaries_for_open_audits()
        except sqlite3.OperationalError:
            return {}

    def _load_hand_flags(self) -> dict[str, dict[RequirementKind, HandFlag]]:
        """Phase 59 §4.3: every part number's hand flags, once per load; a failure costs only the flags."""
        req_repo = getattr(self._audit_repo, "requirements_repo", None)
        if req_repo is None:
            return {}
        try:
            return req_repo.hand_flags_by_part_number()
        except sqlite3.Error as exc:
            logger.warning("Hand flags couldn't be read for the Audit List: %s", type(exc).__name__)
            return {}

    def list_open(self) -> list[OpenAuditDigest]:
        audits = self._audit_repo.list_open()
        summaries = self._load_summaries()
        flags = self._load_hand_flags()

        holidays = self._holiday_svc.list_holidays() if self._holiday_svc else set()
        today_val = date.today()

        return [
            self._map_to_digest(a, holidays, today_val, summaries.get(a.id), flags.get(a.part_number))
            for a in audits
        ]

    def open_audits_for_part_number(self, part_number: str) -> list[OpenAuditDigest]:
        audits = self._audit_repo.list_open_by_part_number(part_number)
        summaries = self._load_summaries()
        flags = self._load_hand_flags()
        holidays = self._holiday_svc.list_holidays() if self._holiday_svc else set()
        today_val = date.today()
        return [
            self._map_to_digest(a, holidays, today_val, summaries.get(a.id), flags.get(a.part_number))
            for a in audits
        ]

    def open_customer_names(self) -> list[str]:
        """Every distinct customer name among open audits, for Settings › Company Names (Phase 59 §5.2).

        pre:  UI thread
        post: trimmed and non-blank, one per company_key (the first spelling in part-number order),
              sorted by key
        raises: sqlite3.Error
        """
        cur = self._audit_repo.conn.cursor()
        cur.execute(
            "SELECT traveler_metadata FROM active_audits ORDER BY part_number, work_order_ref, split_suffix"
        )
        names: dict[str, str] = {}
        for row in cur.fetchall():
            meta = row["traveler_metadata"]
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except (TypeError, ValueError):
                    continue
            customer = _customer_of(meta)
            if customer:
                names.setdefault(company_key(customer), customer)
        return [names[key] for key in sorted(names)]
