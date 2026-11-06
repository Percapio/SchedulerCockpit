"""Phase 55 section 11: SourceFileRepository.replace and PdfComponentCoordRepository.delete_ref_des."""

import pathlib

import pytest

from cockpit.persistence.errors import InvalidArgumentError
from cockpit.persistence.types import SourceFileCategory, SourceFileDraft
from tests.services.phase55_support import bom_line, job_folder, make_shop, write_share_documents


@pytest.fixture
def shop(tmp_path, monkeypatch):
    return make_shop(tmp_path, monkeypatch)


def ingested(shop) -> int:
    documents = write_share_documents(
        job_folder(shop.source_root),
        [bom_line("1", "C-1", ["C1", "C2"]), bom_line("2", "J-1", ["J1"], mount="T")],
        drawing_designators=["C1", "C2", "J1"],
    )
    return shop.ingest(documents)


def draft(audit_id: int, category: SourceFileCategory = SourceFileCategory.BOM) -> SourceFileDraft:
    return SourceFileDraft(audit_id=audit_id, file_category=category, original_filename="revised.xlsx",
                           local_storage_path=pathlib.Path("revised.xlsx"), file_hash="c" * 64)


def test_replace_swaps_the_row_and_its_side_rows_go_with_it(shop):
    audit_id = ingested(shop)
    repositories = shop.repositories
    old = repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.BOM)
    others = {sf.id for sf in repositories.source_files.list_for_audit(audit_id) if sf.id != old.id}

    new = repositories.source_files.replace(audit_id, SourceFileCategory.BOM, draft(audit_id))

    assert repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.BOM) == new
    assert repositories.bom_components.list_for_source_file(old.id) == []
    assert repositories.tht_checklist.list_for_audit(audit_id) == []
    assert {sf.id for sf in repositories.source_files.list_for_audit(audit_id) if sf.id != new.id} == others


def test_replace_refuses_a_draft_for_another_audit_or_category(shop):
    audit_id = ingested(shop)
    with pytest.raises(InvalidArgumentError):
        shop.repositories.source_files.replace(audit_id, SourceFileCategory.BOM, draft(audit_id + 1))
    with pytest.raises(InvalidArgumentError):
        shop.repositories.source_files.replace(audit_id, SourceFileCategory.BOM,
                                               draft(audit_id, SourceFileCategory.NOTES))


def test_delete_ref_des_removes_only_the_named_designators(shop):
    audit_id = ingested(shop)
    drawing = shop.repositories.source_files.find_by_audit_and_category(audit_id, SourceFileCategory.PDF)
    coords = shop.repositories.pdf_coords

    assert coords.delete_ref_des(drawing.id, ["C2", "J1", "NOT-THERE"]) == 2
    assert [c.ref_des for c in coords.list_for_source_file(drawing.id)] == ["C1"]
    assert coords.delete_ref_des(drawing.id, []) == 0
