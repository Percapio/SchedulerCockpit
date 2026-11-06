"""Synthetic documents and a synthetic shop for Phase 55's tests.

Every document is a small JSON file standing in for the real format, read by
stand-in parsers that keep the shipped parsers' signatures. No real BOM, ECO,
traveler or drawing is involved, and every job number, order and name here is
invented.
"""

import json
import os
import pathlib
import threading
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from cockpit.ingestion.errors import (
    MalformedBomError,
    MalformedEcoError,
    MalformedPdfError,
    MalformedTravelerError,
)
from cockpit.ingestion.parsers.results import BomItem, BomResult, EcoResult, TravelerResult
from cockpit.ingestion.service import IngestionService
from cockpit.layout.results import PdfComponentCoordinate, PdfLayoutResult
from cockpit.persistence.connection import open_connection
from cockpit.persistence.repositories.audits import AuditRepository
from cockpit.persistence.repositories.bom_components import AuditBomComponentRepository
from cockpit.persistence.repositories.pdf_coords import PdfComponentCoordRepository
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.persistence.repositories.tht_checklist import ThtChecklistRepository
from cockpit.persistence.schema import migrate
from cockpit.protocols import ParserRegistry
from cockpit.services import document_updates as du
from cockpit.services.runtime_calc import RuntimeCalcService

PART_NUMBER = "B900001"
WORK_ORDER = "SO-1001"

IDENTITY = SimpleNamespace(
    part_number_field="assembly_number",
    work_order_ref_field="sales_order_number",
    quantity_field="quantity",
)
COORD_MAP = SimpleNamespace(identity_mapping=IDENTITY)


# ---------- documents ----------

def bom_line(find_number: str, mpn: str, ref_des: list[str], mount: str = "S",
             description: str | None = None) -> dict[str, Any]:
    return {"find_number": find_number, "component_mpn": mpn, "ref_des": ref_des,
            "mount_type": mount, "description": description}


def bom_text(lines: list[dict[str, Any]], part_number: str = PART_NUMBER) -> str:
    return json.dumps({"declared_part_number": part_number, "items": lines})


def eco_text(part_number: str = PART_NUMBER, revision: str = "A") -> str:
    return json.dumps({"declared_part_number": part_number, "revision": revision})


def traveler_fields(**overrides: Any) -> dict[str, Any]:
    fields = {
        "assembly_number": PART_NUMBER,
        "sales_order_number": WORK_ORDER,
        "quantity": 10,
        "assembly_class": 2,
        "process_clean": "No Clean",
        "customer": "Example Customer",
    }
    fields.update(overrides)
    return fields


def traveler_text(**overrides: Any) -> str:
    return json.dumps({"fields": traveler_fields(**overrides)})


def drawing_text(designators: list[str], page: int = 0) -> str:
    return json.dumps({"designators": designators, "page": page})


def failing_text(reason: str) -> str:
    return json.dumps({"fail": reason})


def write(path: pathlib.Path, text: str, mtime_ns: int | None = None) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _load(path: pathlib.Path, error_type: type) -> dict[str, Any]:
    payload = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    if "fail" in payload:
        raise error_type(pathlib.Path(path), payload["fail"], {})
    return payload


# ---------- stand-in parsers ----------

class JsonBomParser:
    def parse(self, path: pathlib.Path) -> BomResult:
        payload = _load(path, MalformedBomError)
        items = [
            BomItem(
                find_number=line["find_number"], component_mpn=line["component_mpn"],
                description=line["description"], ref_des_raw=",".join(line["ref_des"]),
                ref_des_list=tuple(line["ref_des"]), mount_type=line["mount_type"],
            )
            for line in payload["items"]
        ]
        return BomResult(
            declared_part_number=payload["declared_part_number"], items=items,
            raw_row_count=len(items), excluded_dni_count=0, excluded_pcb_count=0,
            header_layout="canonical",
        )


class JsonEcoParser:
    def parse(self, path: pathlib.Path) -> EcoResult:
        payload = _load(path, MalformedEcoError)
        return EcoResult(declared_part_number=payload["declared_part_number"], row_count=1, raw_table_count=1)


class JsonTravelerParser:
    def parse(self, path: pathlib.Path, coord_map: Any) -> TravelerResult:
        payload = _load(path, MalformedTravelerError)
        return TravelerResult(sheet_name_used="Traveler", extracted_fields=dict(payload["fields"]),
                              raw_anchor_locations={})


class JsonLayoutParser:
    """Finds every designator the drawing lists, one box each, and records its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[pathlib.Path, frozenset[str]]] = []

    def parse(self, path: pathlib.Path, target_ref_des: set[str]) -> PdfLayoutResult:
        self.calls.append((pathlib.Path(path), frozenset(target_ref_des)))
        payload = _load(path, MalformedPdfError)
        found = [d for d in payload["designators"] if d in target_ref_des]
        coordinates = [
            PdfComponentCoordinate(ref_des=d, page_index=payload["page"], x1=float(i), y1=0.0,
                                   x2=float(i) + 1.0, y2=1.0)
            for i, d in enumerate(found)
        ]
        return PdfLayoutResult(coordinates=coordinates, found_ref_des=set(found), page_count=1)


def parser_registry(layout: JsonLayoutParser | None = None) -> ParserRegistry:
    return ParserRegistry(
        bom_parser=JsonBomParser(), eco_parser=JsonEcoParser(), traveler_parser=JsonTravelerParser(),
        pdf_layout_parser=layout or JsonLayoutParser(), coord_map=COORD_MAP,
    )


# ---------- the share ----------

def job_folder(source_root: pathlib.Path, part_number: str = PART_NUMBER, label: str = "Example") -> pathlib.Path:
    folder = source_root / f"{part_number[:4]}xxx" / f"{part_number[:5]}xx" / f"{part_number} {label}"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@dataclass
class ShareDocuments:
    bom: pathlib.Path
    eco: pathlib.Path
    traveler: pathlib.Path
    drawing: pathlib.Path | None


def write_share_documents(
    folder: pathlib.Path,
    lines: list[dict[str, Any]],
    *,
    traveler: dict[str, Any] | None = None,
    drawing_designators: list[str] | None = None,
    part_number: str = PART_NUMBER,
) -> ShareDocuments:
    return ShareDocuments(
        bom=write(folder / f"{part_number} AUDIT BOM.xlsx", bom_text(lines, part_number)),
        eco=write(folder / f"{part_number} ECO.docx", eco_text(part_number)),
        traveler=write(folder / f"{part_number} MFG TRAVELER.xlsx",
                       json.dumps({"fields": traveler or traveler_fields()})),
        drawing=(
            write(folder / f"{part_number} drawing.pdf", drawing_text(drawing_designators))
            if drawing_designators is not None else None
        ),
    )


# ---------- the shop: a real database, real repositories, real ingest ----------

@dataclass
class Shop:
    repositories: du.FamilyUpdateRepositories
    runtime_calc: RuntimeCalcService
    ingestion: IngestionService
    storage_root: pathlib.Path
    source_root: pathlib.Path
    layout: JsonLayoutParser
    parsers: ParserRegistry

    def ingest(self, documents: ShareDocuments, quantity: int | None = None) -> int:
        """Ingests one family through the shipped IngestionService, with the stand-in parsers."""
        paths = [documents.bom, documents.eco, documents.traveler]
        if documents.drawing is not None:
            paths.append(documents.drawing)
        audit = self.ingestion.ingest(paths)
        if quantity is not None:
            self.repositories.audits.set_quantity(audit.id, quantity)
        return audit.id

    def split(self, audit_id: int, suffix: str, quantity: int) -> int:
        return self.repositories.audits.clone_to_suffix(audit_id, suffix, quantity, "test split").id

    def snapshot(self, key: du.FamilyKey | None = None) -> du.FamilySnapshot:
        snapshot = du.snapshot_family(key or du.FamilyKey(PART_NUMBER, WORK_ORDER), self.repositories)
        assert snapshot is not None
        return snapshot

    def check(self, choices=None) -> du.FamilyPreview:
        probe = du.probe_family(self.snapshot(), self.source_root)
        assert isinstance(probe, du.FamilyProbe), probe
        return du.build_preview(probe, choices or {}, self.parsers, self.source_root)

    def prepare(self, preview: du.FamilyPreview, stop: threading.Event | None = None, **kwargs):
        return du.prepare_family_update(
            preview, self.storage_root, self.source_root, self.parsers,
            stop or threading.Event(), "run1", **kwargs,
        )

    def apply(self, plan: du.FamilyUpdatePlan, refresh=None):
        return du.apply_family_update(plan, self.repositories, self.runtime_calc, refresh)


def install_stand_in_parsers(monkeypatch) -> None:
    """Points the shipped ingest at the stand-in parsers; reconcile stays real."""
    monkeypatch.setattr("cockpit.ingestion.parsers.audit_bom.parse", JsonBomParser().parse)
    monkeypatch.setattr("cockpit.ingestion.parsers.eco_build_notes.parse", JsonEcoParser().parse)
    monkeypatch.setattr("cockpit.ingestion.parsers.traveler.parse", JsonTravelerParser().parse)


def make_shop(tmp_path: pathlib.Path, monkeypatch) -> Shop:
    install_stand_in_parsers(monkeypatch)
    conn = open_connection(tmp_path / "cockpit.db")
    layout = JsonLayoutParser()
    registry = parser_registry(layout)
    migrate(conn, registry)

    bom_components = AuditBomComponentRepository(conn)
    pdf_coords = PdfComponentCoordRepository(conn)
    audits = AuditRepository(conn, bom_components, pdf_coords)
    source_files = SourceFileRepository(conn)
    tht_checklist = ThtChecklistRepository(conn)
    runtime_calc = RuntimeCalcService(audits, source_files, bom_components, pdf_coords)

    storage_root = tmp_path / "storage"
    ingestion = IngestionService(
        conn=conn, audit_repo=audits, source_file_repo=source_files, tht_repo=tht_checklist,
        bom_component_repo=bom_components, pdf_coord_repo=pdf_coords, layout_parser=layout,
        coord_map=COORD_MAP, file_storage_root=storage_root, runtime_calc_svc=runtime_calc,
    )
    return Shop(
        repositories=du.FamilyUpdateRepositories(
            conn=conn, audits=audits, source_files=source_files, bom_components=bom_components,
            tht_checklist=tht_checklist, pdf_coords=pdf_coords,
        ),
        runtime_calc=runtime_calc,
        ingestion=ingestion,
        storage_root=storage_root,
        source_root=tmp_path / "share",
        layout=layout,
        parsers=registry,
    )
