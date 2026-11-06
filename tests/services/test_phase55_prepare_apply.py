"""Phase 55 sections 5-6: prepare on the worker, apply on the UI thread, against a real database.

Families are ingested through the shipped IngestionService with stand-in
parsers, so storage, copy2 and the side-table rows are exactly ingest's.
"""

import dataclasses
import logging
import pathlib
import shutil
import threading

import pytest

from cockpit.ingestion import hashing
from cockpit.ingestion.errors import MalformedPdfError
from cockpit.ingestion.roles import SourceRole
from cockpit.persistence.types import SourceFileCategory
from cockpit.protocols import ParserRegistry
from cockpit.services import document_updates as du
from cockpit.services.startup_reconciler import StartupReconciler
from tests.services.phase55_support import (
    COORD_MAP,
    PART_NUMBER,
    WORK_ORDER,
    JsonBomParser,
    JsonEcoParser,
    JsonTravelerParser,
    bom_line,
    bom_text,
    eco_text,
    job_folder,
    make_shop,
    traveler_fields,
    traveler_text,
    write,
    write_share_documents,
)

LINES = [
    bom_line("1", "C-1", ["C1", "C2"]),
    bom_line("2", "R-1", ["R1"]),
    bom_line("3", "J-1", ["J1"], mount="T"),
]
REVISED = [
    bom_line("1", "C-1", ["C1", "C2"]),
    bom_line("3", "J-1", ["J1"], mount="T"),
    bom_line("4", "U-9", ["U9"]),
]
DRAWING = ["C1", "C2", "R1", "J1", "U9"]
REVISED_NAME = f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx"


@pytest.fixture
def shop(tmp_path, monkeypatch):
    return make_shop(tmp_path, monkeypatch)


def ingest_family(shop, siblings: int = 0, lines=LINES):
    documents = write_share_documents(job_folder(shop.source_root), lines, drawing_designators=DRAWING)
    first = shop.ingest(documents)
    ids = [first] + [shop.split(first, f"-{n + 2}", 1) for n in range(siblings)]
    shop.layout.calls.clear()
    return documents, ids


def revise_bom(shop, lines=REVISED) -> pathlib.Path:
    return write(job_folder(shop.source_root) / REVISED_NAME, bom_text(lines))


def row(shop, audit_id: int, category: SourceFileCategory):
    return shop.repositories.source_files.find_by_audit_and_category(audit_id, category)


def designators(shop, audit_id: int) -> set[str]:
    bom = row(shop, audit_id, SourceFileCategory.BOM)
    return {c.ref_des for c in shop.repositories.bom_components.list_for_source_file(bom.id)}


def coordinates(shop, audit_id: int) -> set[str]:
    drawing = row(shop, audit_id, SourceFileCategory.PDF)
    return {c.ref_des for c in shop.repositories.pdf_coords.list_for_source_file(drawing.id)}


def family_state(shop, ids: list[int]) -> dict:
    conn = shop.repositories.conn
    marks = ",".join("?" * len(ids))
    return {
        "audits": conn.execute(f"SELECT * FROM active_audits WHERE id IN ({marks}) ORDER BY id", ids).fetchall(),
        "files": conn.execute(f"SELECT * FROM source_files WHERE audit_id IN ({marks}) ORDER BY id", ids).fetchall(),
        "components": conn.execute(
            f"SELECT c.* FROM audit_bom_components c JOIN source_files s ON s.id = c.source_file_id "
            f"WHERE s.audit_id IN ({marks}) ORDER BY c.id", ids).fetchall(),
        "tht": conn.execute(f"SELECT * FROM tht_verification_checklist WHERE audit_id IN ({marks}) ORDER BY id",
                            ids).fetchall(),
        "coords": conn.execute(
            f"SELECT c.* FROM pdf_component_coords c JOIN source_files s ON s.id = c.source_file_id "
            f"WHERE s.audit_id IN ({marks}) ORDER BY c.id", ids).fetchall(),
    }


def partial_files(shop) -> list[pathlib.Path]:
    return list(shop.storage_root.rglob(f"*{du.PARTIAL_COPY_SUFFIX}"))


def prepared(shop) -> du.FamilyUpdatePlan:
    plan = shop.prepare(shop.check())
    assert isinstance(plan, du.FamilyUpdatePlan), plan
    return plan


# ---------- apply ----------

def test_a_three_sibling_family_is_updated_as_one(shop):
    documents, ids = ingest_family(shop, siblings=2)
    old_bom = row(shop, ids[0], SourceFileCategory.BOM)
    revised = revise_bom(shop)

    preview = shop.check()
    assert preview.probe.statuses[SourceRole.BOM].members_to_update == tuple(ids)
    outcome = shop.apply(shop.prepare(preview))

    assert isinstance(outcome, du.FamilyUpdated)
    assert outcome.replaced == (SourceRole.BOM,)
    for audit_id in ids:
        bom = row(shop, audit_id, SourceFileCategory.BOM)
        assert bom.original_filename == REVISED_NAME
        assert bom.file_hash == hashing.sha256_hex(revised)
        assert designators(shop, audit_id) == {"C1", "C2", "J1", "U9"}
        tht = shop.repositories.tht_checklist.list_for_audit(audit_id)
        assert [(t.component_mpn, t.source_file_id) for t in tht] == [("J-1", bom.id)]
    assert not old_bom.local_storage_path.exists()
    assert outcome.reaped_paths == (old_bom.local_storage_path,)
    assert shop.check().probe.statuses[SourceRole.BOM] == du.Unchanged()


def test_the_cascade_removes_the_old_rows(shop):
    documents, ids = ingest_family(shop)
    old_bom = row(shop, ids[0], SourceFileCategory.BOM)
    revise_bom(shop)
    shop.apply(prepared(shop))
    conn = shop.repositories.conn
    assert conn.execute("SELECT COUNT(*) AS n FROM audit_bom_components WHERE source_file_id = ?",
                        (old_bom.id,)).fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM tht_verification_checklist WHERE source_file_id = ?",
                        (old_bom.id,)).fetchone()["n"] == 0


def test_operator_state_is_never_touched(shop):
    documents, ids = ingest_family(shop, siblings=1)
    conn = shop.repositories.conn
    conn.execute(
        "UPDATE active_audits SET status = 'SMT', ship_date = '2026-12-01', general_notes = 'keep me', "
        "ops_per_board_min = 2.5, is_labeled = 1, are_photos_uploaded = 1 WHERE id IN (?, ?)", ids)
    shop.repositories.audits.set_quantity(ids[0], 7)
    kept = ("status", "ship_date", "general_notes", "ops_per_board_min", "is_labeled", "are_photos_uploaded",
            "quantity", "article_revision", "split_reason", "part_number", "work_order_ref", "split_suffix")
    before = {a: {k: getattr(shop.repositories.audits.find_by_id(a), k) for k in kept} for a in ids}

    revise_bom(shop)
    write(job_folder(shop.source_root) / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx",
          traveler_text(quantity=99, process_clean="Clean"))
    outcome = shop.apply(prepared(shop))

    assert isinstance(outcome, du.FamilyUpdated)
    after = {a: {k: getattr(shop.repositories.audits.find_by_id(a), k) for k in kept} for a in ids}
    assert after == before


def test_a_traveler_update_recomputes_flags_and_runtimes_but_never_the_quantity(shop):
    documents, ids = ingest_family(shop)
    before = shop.repositories.audits.find_by_id(ids[0])
    assert not before.is_clean_process and not before.is_class_3

    write(job_folder(shop.source_root) / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx",
          traveler_text(quantity=12, process_clean="Clean", assembly_class=3))
    outcome = shop.apply(prepared(shop))

    after = shop.repositories.audits.find_by_id(ids[0])
    assert after.is_clean_process and after.is_class_3
    assert after.quantity == 10
    assert after.traveler_metadata["quantity"] == 12
    assert outcome.quantity_not_applied == (12, 10)
    shop.runtime_calc.persist(ids[0])
    recomputed = shop.repositories.audits.find_by_id(ids[0])
    assert after.tht_runtime == recomputed.tht_runtime != before.tht_runtime


def test_family_changed_since_preview_discards_this_runs_copies(shop):
    documents, ids = ingest_family(shop)
    revise_bom(shop)
    plan = prepared(shop)
    staged = plan.staged[0]
    assert staged.copied_by_this_run and staged.stored_path.exists()
    before = family_state(shop, ids)

    sibling = shop.split(ids[0], "-2", 1)
    outcome = shop.apply(plan)

    assert outcome == du.FamilyChangedSincePreview(plan.family)
    assert not staged.stored_path.exists()
    assert family_state(shop, ids) == before
    assert row(shop, sibling, SourceFileCategory.BOM).original_filename == documents.bom.name


def test_a_failed_write_rolls_back_to_exactly_what_was_there(shop, monkeypatch):
    documents, ids = ingest_family(shop, siblings=1)
    revise_bom(shop)
    plan = prepared(shop)
    before = family_state(shop, ids)

    def disk_full(items):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shop.repositories.tht_checklist, "insert_many", disk_full)
    outcome = shop.apply(plan)

    assert outcome == du.WriteFailed("OSError (28)")
    assert family_state(shop, ids) == before
    assert not plan.staged[0].stored_path.exists()
    assert not shop.repositories.conn.in_transaction


def test_superseded_files_are_reaped_only_when_unreferenced(shop):
    documents, ids = ingest_family(shop)
    other = write_share_documents(shop.source_root.parent / "other order", LINES,
                                  traveler=traveler_fields(sales_order_number="SO-2002"))
    other_id = shop.ingest(other)
    old_bom = row(shop, ids[0], SourceFileCategory.BOM)
    assert row(shop, other_id, SourceFileCategory.BOM).local_storage_path == old_bom.local_storage_path

    revise_bom(shop)
    outcome = shop.apply(prepared(shop))

    assert outcome.reaped_paths == ()
    assert old_bom.local_storage_path.exists()
    assert row(shop, other_id, SourceFileCategory.BOM).file_hash == old_bom.file_hash


def test_a_lagging_member_is_found_and_completed(shop):
    documents, ids = ingest_family(shop, siblings=1)
    old_bom = row(shop, ids[0], SourceFileCategory.BOM)
    revise_bom(shop)
    plan = prepared(shop)
    only_second = dataclasses.replace(
        plan,
        members_to_update={SourceRole.BOM: (ids[1],)},
        expected_stored_hashes={ids[1]: plan.expected_stored_hashes[ids[1]]},
        pdf_coordinates={a: c for a, c in plan.pdf_coordinates.items() if a == ids[1]},
    )
    assert isinstance(shop.apply(only_second), du.FamilyUpdated)
    assert old_bom.local_storage_path.exists()

    status = shop.check().probe.statuses[SourceRole.BOM]
    assert isinstance(status, du.Changed)
    assert status.members_to_update == (ids[0],)

    assert isinstance(shop.apply(prepared(shop)), du.FamilyUpdated)
    assert {row(shop, a, SourceFileCategory.BOM).original_filename for a in ids} == {REVISED_NAME}
    assert not old_bom.local_storage_path.exists()


# ---------- the requirements hook ----------

def test_the_requirements_hook_is_called_once_after_release(shop):
    documents, ids = ingest_family(shop, siblings=1)
    revised = revise_bom(shop)
    calls = []

    def refresh(audit_ids):
        calls.append((tuple(audit_ids), shop.repositories.conn.in_transaction,
                      row(shop, ids[0], SourceFileCategory.BOM).file_hash))

    shop.apply(prepared(shop), refresh=refresh)
    assert calls == [(tuple(ids), False, hashing.sha256_hex(revised))]


def test_a_raising_hook_is_logged_and_the_update_stands(shop, caplog):
    documents, ids = ingest_family(shop)
    revise_bom(shop)

    def refresh(audit_ids):
        raise RuntimeError("reconciler not started")

    with caplog.at_level(logging.WARNING, logger="cockpit.services.document_updates"):
        outcome = shop.apply(prepared(shop), refresh=refresh)
    assert isinstance(outcome, du.FamilyUpdated)
    assert row(shop, ids[0], SourceFileCategory.BOM).original_filename == REVISED_NAME
    assert "Requirements refresh request failed" in caplog.text
    assert "reconciler not started" not in caplog.text


# ---------- prepare ----------

def test_a_share_that_changed_after_the_preview_is_refused(shop):
    ingest_family(shop)
    revised = revise_bom(shop)
    preview = shop.check()
    write(revised, bom_text(REVISED + [bom_line("9", "X-1", ["X1"])]))

    outcome = shop.prepare(preview)
    assert outcome == du.PrepareFailed(du.CandidateChangedSincePreview(SourceRole.BOM), ())
    assert partial_files(shop) == []
    assert not any(p.name.startswith(revised.stem) for p in shop.storage_root.rglob("*"))


def test_an_interrupted_copy_never_leaves_a_file_at_its_destination(shop):
    ingest_family(shop)
    revise_bom(shop)

    def interrupted(source, destination):
        pathlib.Path(destination).write_bytes(b"half a file")
        raise OSError(64, "The specified network name is no longer available")

    outcome = shop.prepare(shop.check(), copy_file=interrupted)
    assert outcome == du.PrepareFailed(du.StorageFailed("OSError (64)"), ())
    assert partial_files(shop) == []
    assert not any(p.name.startswith(pathlib.Path(REVISED_NAME).stem) for p in shop.storage_root.rglob("*"))


def test_cancel_before_anything_is_copied(shop):
    ingest_family(shop)
    revise_bom(shop)
    stop = threading.Event()
    stop.set()
    assert shop.prepare(shop.check(), stop=stop) == du.PrepareFailed(du.Cancelled(), ())


def test_cancel_during_a_copy_discards_it(shop):
    ingest_family(shop)
    revise_bom(shop)
    stop = threading.Event()

    def copy_then_cancel(source, destination):
        shutil.copy2(source, destination)
        stop.set()

    assert shop.prepare(shop.check(), stop=stop, copy_file=copy_then_cancel) == du.PrepareFailed(du.Cancelled(), ())
    assert partial_files(shop) == []


def test_cancel_after_the_copies_hands_them_back_for_the_ui_thread(shop):
    ingest_family(shop)
    revise_bom(shop)
    stop = threading.Event()

    class CancellingBomParser(JsonBomParser):
        def parse(self, path):
            stop.set()
            return super().parse(path)

    registry = ParserRegistry(CancellingBomParser(), JsonEcoParser(), JsonTravelerParser(), shop.layout, COORD_MAP)
    outcome = du.prepare_family_update(shop.check(), shop.storage_root, shop.source_root, registry, stop, "run1")

    assert isinstance(outcome, du.PrepareFailed) and outcome.failure == du.Cancelled()
    (copy,) = outcome.copied_by_this_run
    assert copy.stored_path.exists()
    assert du.discard_copies(outcome.copied_by_this_run, shop.repositories.source_files) == (copy.stored_path,)
    assert not copy.stored_path.exists()


def test_the_identity_checks_repeat_on_the_stored_copies(shop):
    ingest_family(shop)
    write(job_folder(shop.source_root) / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx", traveler_text(quantity=11))

    class StoredCopyNamesAnotherOrder(JsonTravelerParser):
        def parse(self, path, coord_map):
            result = super().parse(path, coord_map)
            if shop.storage_root in pathlib.Path(path).parents:
                result.extracted_fields["sales_order_number"] = "SO-9999"
            return result

    registry = ParserRegistry(JsonBomParser(), JsonEcoParser(), StoredCopyNamesAnotherOrder(), shop.layout, COORD_MAP)
    preview = du.build_preview(du.probe_family(shop.snapshot(), shop.source_root), {}, registry, shop.source_root)
    assert preview.can_apply

    outcome = du.prepare_family_update(preview, shop.storage_root, shop.source_root, registry, threading.Event(), "run1")
    assert outcome.failure == du.IdentityMismatch(SourceRole.TRAVELER, "sales_order_number", WORK_ORDER, "SO-9999")
    assert [c.role for c in outcome.copied_by_this_run] == [SourceRole.TRAVELER]


def test_an_existing_destination_is_verified_and_not_copied_again(shop):
    ingest_family(shop)
    revise_bom(shop)
    first = prepared(shop)
    assert first.staged[0].copied_by_this_run

    second = prepared(shop)
    assert second.staged[0].stored_path == first.staged[0].stored_path
    assert not second.staged[0].copied_by_this_run


# ---------- the drawing ----------

def test_the_drawing_is_not_read_for_an_eco_only_update(shop):
    ingest_family(shop)
    write(job_folder(shop.source_root) / f"{PART_NUMBER} ECO_11-04-26.docx", eco_text(revision="B"))
    plan = prepared(shop)
    assert plan.pdf_coordinates == {} and shop.layout.calls == []
    assert isinstance(shop.apply(plan), du.FamilyUpdated)


def test_the_drawing_is_not_read_when_the_designators_are_kept(shop):
    ingest_family(shop)
    revise_bom(shop, [dict(line, description="re-described") for line in LINES])
    plan = prepared(shop)
    assert plan.pdf_coordinates == {} and shop.layout.calls == []


def test_changed_designators_re_read_the_drawing_for_the_added_ones_only(shop):
    documents, ids = ingest_family(shop)
    assert coordinates(shop, ids[0]) == {"C1", "C2", "R1", "J1"}
    revise_bom(shop)
    plan = prepared(shop)

    assert [targets for _path, targets in shop.layout.calls] == [frozenset({"U9"})]
    assert isinstance(shop.apply(plan), du.FamilyUpdated)
    assert coordinates(shop, ids[0]) == {"C1", "C2", "J1", "U9"}


def test_a_drawing_that_cannot_be_re_read_degrades_without_failing(shop, monkeypatch):
    documents, ids = ingest_family(shop)
    revise_bom(shop)

    def unreadable(path, targets):
        raise MalformedPdfError(pathlib.Path(path), "UNREADABLE_PDF", {})

    monkeypatch.setattr(shop.layout, "parse", unreadable)
    plan = prepared(shop)
    assert plan.warnings == (du.DrawingHighlightsIncomplete("MalformedPdfError (UNREADABLE_PDF)", 1),)

    outcome = shop.apply(plan)
    assert outcome.warnings == plan.warnings
    assert coordinates(shop, ids[0]) == {"C1", "C2", "J1"}


# ---------- cleanup, crash recovery, and the UI thread's reads ----------

def test_a_copy_another_row_references_survives_cleanup(shop):
    documents, ids = ingest_family(shop)
    bom = row(shop, ids[0], SourceFileCategory.BOM)
    staged = du.StagedDocument(SourceRole.BOM, bom.original_filename, bom.local_storage_path, bom.file_hash, True)
    assert du.discard_copies((staged,), shop.repositories.source_files) == ()
    assert bom.local_storage_path.exists()


def test_an_unlink_that_fails_is_logged_and_left(shop, monkeypatch, caplog):
    ingest_family(shop)
    revise_bom(shop)
    plan = prepared(shop)
    shipped_unlink = pathlib.Path.unlink

    def locked(self, missing_ok=False):
        if self == plan.staged[0].stored_path:
            raise PermissionError(13, "The process cannot access the file")
        return shipped_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", locked)
    with caplog.at_level(logging.WARNING, logger="cockpit.services.document_updates"):
        assert du.discard_copies(plan.staged, shop.repositories.source_files) == ()
    assert plan.staged[0].stored_path.exists()
    assert "startup sweep" in caplog.text


def test_a_crash_between_copy_and_apply_is_swept_at_the_next_start(shop):
    documents, ids = ingest_family(shop)
    revise_bom(shop)
    plan = prepared(shop)
    staged_path = plan.staged[0].stored_path

    report = StartupReconciler(shop.repositories.audits, shop.repositories.source_files, None,
                               shop.storage_root, hashing.sha256_hex).reconcile()
    assert staged_path in report.orphans_deleted
    assert row(shop, ids[0], SourceFileCategory.BOM).local_storage_path.exists()

    assert isinstance(shop.check().probe.statuses[SourceRole.BOM], du.Changed)
    assert isinstance(shop.apply(prepared(shop)), du.FamilyUpdated)


def test_the_snapshot_names_the_reference_member_and_every_members_documents(shop):
    documents, ids = ingest_family(shop, siblings=1)
    snapshot = shop.snapshot()
    assert snapshot.reference_member == ids[0]
    assert set(snapshot.stored) == set(ids)
    assert set(snapshot.stored[ids[1]]) == {SourceRole.BOM, SourceRole.NOTES, SourceRole.TRAVELER, SourceRole.PDF}
    assert snapshot.members[1].designators == frozenset({"C1", "C2", "R1", "J1"})
    assert snapshot.held.tht_items == (("J-1", None),)

    shop.repositories.audits.relabel_suffix(ids[0], "-1")
    assert shop.snapshot().reference_member == min(ids)
    assert du.open_family_keys(shop.repositories.audits) == [du.FamilyKey(PART_NUMBER, WORK_ORDER)]


def test_a_family_that_no_longer_exists_has_no_snapshot(shop):
    assert du.snapshot_family(du.FamilyKey(PART_NUMBER, WORK_ORDER), shop.repositories) is None
