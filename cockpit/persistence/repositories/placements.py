"""Repositories for Phase 57's placements tables (Architecture/20261115-Phase57.md §7)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable, Sequence

from cockpit.placements.bmb_file import (
    BmbPlacement, BodyRectangle, PlacementTriplet, rejection_from_json, rejection_to_json,
)
from cockpit.placements.registration import (
    BoardToPage, LabelsDisagree, LabelsOffPart, MixedSides, NoFit, PageOutcome, PageRegistration,
    Refused, Registered, TooFewLabels, TooManyLabels,
)
from ..types import PlacementRejection

REFUSAL_TYPES: dict[str, type] = {
    cls.__name__: cls for cls in (TooFewLabels, TooManyLabels, NoFit, LabelsDisagree, LabelsOffPart, MixedSides)
}


class BoardPlacementRepository:
    """board_placements: one Placements row's admitted placements."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def insert_many(self, placement_file_id: int, placements: Sequence[BmbPlacement]) -> int:
        rows = [
            (
                placement_file_id, p.designator, p.placement.x_mils, p.placement.y_mils,
                p.placement.rotation_ccw_degrees, p.side, 1 if p.is_stuffed else 0, p.mount,
                p.part_number, p.package_key,
                p.body.centre_x_mils if p.body else None, p.body.centre_y_mils if p.body else None,
                p.body.width_mils if p.body else None, p.body.height_mils if p.body else None,
            )
            for p in placements
        ]
        self.conn.executemany(
            """
            INSERT INTO board_placements (
                placement_file_id, designator, x_mils, y_mils, rotation_ccw, side, is_stuffed, mount,
                part_number, package_key, body_cx_mils, body_cy_mils, body_w_mils, body_h_mils
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return len(rows)

    def list_for_file(self, placement_file_id: int) -> list[BmbPlacement]:
        cur = self.conn.execute(
            "SELECT * FROM board_placements WHERE placement_file_id = ? ORDER BY id ASC",
            (placement_file_id,),
        )
        out: list[BmbPlacement] = []
        for row in cur.fetchall():
            body = None
            if row["body_w_mils"] is not None:
                body = BodyRectangle(row["body_cx_mils"], row["body_cy_mils"], row["body_w_mils"], row["body_h_mils"])
            out.append(BmbPlacement(
                designator=row["designator"],
                placement=PlacementTriplet(row["x_mils"], row["y_mils"], row["rotation_ccw"]),
                side=row["side"],
                is_stuffed=bool(row["is_stuffed"]),
                mount=row["mount"],
                part_number=row["part_number"],
                package_key=row["package_key"],
                body=body,
            ))
        return out

    def count_for_file(self, placement_file_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM board_placements WHERE placement_file_id = ?", (placement_file_id,)
        ).fetchone()
        return row["n"] if row else 0

    def delete_for_file(self, placement_file_id: int) -> int:
        return self.conn.execute(
            "DELETE FROM board_placements WHERE placement_file_id = ?", (placement_file_id,)
        ).rowcount

    def clone_for_file(self, source_id: int, target_id: int) -> int:
        return self.conn.execute(
            """
            INSERT INTO board_placements (
                placement_file_id, designator, x_mils, y_mils, rotation_ccw, side, is_stuffed, mount,
                part_number, package_key, body_cx_mils, body_cy_mils, body_w_mils, body_h_mils
            )
            SELECT ?, designator, x_mils, y_mils, rotation_ccw, side, is_stuffed, mount,
                   part_number, package_key, body_cx_mils, body_cy_mils, body_w_mils, body_h_mils
            FROM board_placements WHERE placement_file_id = ? ORDER BY id ASC
            """,
            (target_id, source_id),
        ).rowcount


def _reasons_json(reasons: Iterable) -> str:
    return json.dumps([{"kind": type(r).__name__, **vars(r)} for r in reasons], sort_keys=True)


def _reasons_from_json(text: str) -> tuple:
    out = []
    for item in json.loads(text or "[]"):
        cls = REFUSAL_TYPES.get(item.get("kind", ""))
        if cls is None:
            continue
        fields = {k: v for k, v in item.items() if k != "kind"}
        try:
            out.append(cls(**fields))
        except TypeError:
            continue
    return tuple(out)


class PageRegistrationRepository:
    """drawing_page_registrations: one drawing's pages against one Placements row."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def replace(self, drawing_file_id: int, placement_file_id: int, outcomes: Sequence[PageOutcome]) -> None:
        """Replaces the pair's registrations: exactly one row per outcome, none from before.

        pre:  inside the caller's savepoint
        """
        self.conn.execute(
            "DELETE FROM drawing_page_registrations WHERE drawing_file_id = ? AND placement_file_id = ?",
            (drawing_file_id, placement_file_id),
        )
        rows = []
        for outcome in outcomes:
            if isinstance(outcome, Registered):
                r = outcome.registration
                t = r.board_to_page
                rows.append((drawing_file_id, placement_file_id, r.page_index, "Registered", r.side,
                             t.scale_cos, t.scale_sin, t.offset_x_pt, t.offset_y_pt, 1 if t.mirrored else 0,
                             r.inliers, r.matched_labels, r.residual_p90_mils, r.candidates_tried, "[]"))
            else:
                tried = outcome.best.candidates_tried if outcome.best is not None else 0
                rows.append((drawing_file_id, placement_file_id, outcome.page_index, "Refused", outcome.side,
                             None, None, None, None, None, None, outcome.matched_labels, None, tried,
                             _reasons_json(outcome.reasons)))
        self.conn.executemany(
            """
            INSERT INTO drawing_page_registrations (
                drawing_file_id, placement_file_id, page_index, outcome, side, scale_cos, scale_sin,
                offset_x_pt, offset_y_pt, mirrored, inliers, matched_labels, residual_p90_mils,
                candidates_tried, refusal_reasons
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    def list_for(self, drawing_file_id: int, placement_file_id: int) -> list[PageOutcome]:
        cur = self.conn.execute(
            """
            SELECT * FROM drawing_page_registrations
            WHERE drawing_file_id = ? AND placement_file_id = ? ORDER BY page_index ASC
            """,
            (drawing_file_id, placement_file_id),
        )
        out: list[PageOutcome] = []
        for row in cur.fetchall():
            if row["outcome"] == "Registered":
                out.append(Registered(PageRegistration(
                    page_index=row["page_index"],
                    side=row["side"],
                    board_to_page=BoardToPage(row["scale_cos"], row["scale_sin"], row["offset_x_pt"],
                                              row["offset_y_pt"], bool(row["mirrored"])),
                    inliers=row["inliers"],
                    matched_labels=row["matched_labels"],
                    residual_p90_mils=row["residual_p90_mils"],
                    candidates_tried=row["candidates_tried"] or 0,
                )))
            else:
                out.append(Refused(row["page_index"], row["side"], row["matched_labels"],
                                   _reasons_from_json(row["refusal_reasons"])))
        return out

    def delete_for_placement_file(self, placement_file_id: int) -> int:
        return self.conn.execute(
            "DELETE FROM drawing_page_registrations WHERE placement_file_id = ?", (placement_file_id,)
        ).rowcount

    def clone(self, source_drawing_id: int, source_placement_id: int, target_drawing_id: int,
              target_placement_id: int) -> int:
        return self.conn.execute(
            """
            INSERT INTO drawing_page_registrations (
                drawing_file_id, placement_file_id, page_index, outcome, side, scale_cos, scale_sin,
                offset_x_pt, offset_y_pt, mirrored, inliers, matched_labels, residual_p90_mils,
                candidates_tried, refusal_reasons
            )
            SELECT ?, ?, page_index, outcome, side, scale_cos, scale_sin, offset_x_pt, offset_y_pt,
                   mirrored, inliers, matched_labels, residual_p90_mils, candidates_tried, refusal_reasons
            FROM drawing_page_registrations WHERE drawing_file_id = ? AND placement_file_id = ?
            ORDER BY id ASC
            """,
            (target_drawing_id, target_placement_id, source_drawing_id, source_placement_id),
        ).rowcount

    def pairs_for_files(self, file_ids: Iterable[int]) -> list[tuple[int, int]]:
        ids = list(file_ids)
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        cur = self.conn.execute(
            f"""
            SELECT DISTINCT drawing_file_id, placement_file_id FROM drawing_page_registrations
            WHERE drawing_file_id IN ({marks}) AND placement_file_id IN ({marks})
            """,
            ids + ids,
        )
        return [(row["drawing_file_id"], row["placement_file_id"]) for row in cur.fetchall()]


def _instant(value: datetime | str) -> datetime:
    """seen_at as an aware instant: the connection's row factory hydrates *_at columns, a bare one doesn't."""
    instant = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=timezone.utc)


class PlacementRejectionRepository:
    """placement_rejections: share files rejected or refused for an audit, recorded, not stored."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def record(self, audit_id: int, rejection: PlacementRejection) -> None:
        """One row per (audit, share_hash); recording the same hash again updates it."""
        self.conn.execute(
            """
            INSERT INTO placement_rejections (
                audit_id, share_file_name, share_hash, share_size, share_modified_ns, reason, bom_hash, seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(audit_id, share_hash) DO UPDATE SET
                share_file_name = excluded.share_file_name,
                share_size = excluded.share_size,
                share_modified_ns = excluded.share_modified_ns,
                reason = excluded.reason,
                bom_hash = excluded.bom_hash,
                seen_at = excluded.seen_at
            """,
            (
                audit_id, rejection.share_file_name, rejection.share_hash, rejection.share_size,
                rejection.share_modified_ns, json.dumps(rejection_to_json(rejection.reason), sort_keys=True),
                rejection.bom_hash, rejection.seen_at.isoformat(),
            ),
        )

    def list_for_audit(self, audit_id: int) -> list[PlacementRejection]:
        cur = self.conn.execute(
            "SELECT * FROM placement_rejections WHERE audit_id = ? ORDER BY seen_at DESC, id DESC",
            (audit_id,),
        )
        out = []
        for row in cur.fetchall():
            try:
                reason = rejection_from_json(json.loads(row["reason"]))
            except (ValueError, TypeError):
                reason = rejection_from_json({"kind": "Unreadable"})
            out.append(PlacementRejection(
                share_file_name=row["share_file_name"],
                share_hash=row["share_hash"],
                share_size=row["share_size"],
                share_modified_ns=row["share_modified_ns"],
                reason=reason,
                bom_hash=row["bom_hash"],
                seen_at=_instant(row["seen_at"]),
            ))
        return out

    def clone_for_audit(self, source_audit_id: int, target_audit_id: int) -> int:
        return self.conn.execute(
            """
            INSERT INTO placement_rejections (
                audit_id, share_file_name, share_hash, share_size, share_modified_ns, reason, bom_hash, seen_at
            )
            SELECT ?, share_file_name, share_hash, share_size, share_modified_ns, reason, bom_hash, seen_at
            FROM placement_rejections WHERE audit_id = ? ORDER BY id ASC
            """,
            (target_audit_id, source_audit_id),
        ).rowcount
