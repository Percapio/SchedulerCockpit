"""Phase 55 section 4: the preview's dry run."""

import pathlib

from cockpit.ingestion import hashing
from cockpit.ingestion.roles import SourceRole
from cockpit.ingestion.version_tags import version_tag
from cockpit.persistence.types import AuditBomComponent
from cockpit.services import document_updates as du
from tests.services.phase55_support import (
    PART_NUMBER,
    WORK_ORDER,
    bom_line,
    bom_text,
    eco_text,
    failing_text,
    job_folder,
    parser_registry,
    traveler_fields,
    traveler_text,
    write,
)

OLD_HASH = "a" * 64


def component(find_number: str, mpn: str, ref_des: str, mount: str = "S") -> AuditBomComponent:
    return AuditBomComponent(id=0, source_file_id=1, component_mpn=mpn, ref_des=ref_des, mount_type=mount,
                             description=None, find_number=find_number)


HELD_COMPONENTS = (
    component("1", "C-1", "C1"),
    component("1", "C-1", "C2"),
    component("2", "R-1", "R1"),
    component("3", "J-1", "J1", mount="T"),
)


def held(**overrides) -> du.HeldState:
    values = dict(
        components=HELD_COMPONENTS,
        tht_items=(("J-1", None),),
        traveler_metadata=traveler_fields(),
        ops_per_board_min=None,
    )
    values.update(overrides)
    return du.HeldState(**values)


def file_of(path: pathlib.Path) -> du.CandidateFile:
    stat = path.stat()
    return du.CandidateFile(path=path, name=path.name, size=stat.st_size, modified_at_ns=stat.st_mtime_ns,
                            tag=version_tag(path.name))


def stored_documents(members=(1,)) -> dict:
    return {
        audit_id: {
            role: du.StoredDocument(role=role, source_file_id=audit_id * 10 + i, original_filename=f"old {role.value}",
                                    stored_path=pathlib.Path("storage") / role.value, file_hash=OLD_HASH,
                                    size=1, modified_at_ns=1)
            for i, role in enumerate(du.UPDATED_ROLES)
        }
        for audit_id in members
    }


def probe_with(statuses: dict, tmp_path: pathlib.Path, held_state: du.HeldState | None = None,
               members=((1, "", 10),)) -> du.FamilyProbe:
    full = {role: du.Unchanged() for role in du.UPDATED_ROLES}
    full.update(statuses)
    snapshot = du.FamilySnapshot(
        key=du.FamilyKey(PART_NUMBER, WORK_ORDER),
        members=tuple(du.FamilyMember(a, s, q, frozenset()) for a, s, q in members),
        reference_member=members[0][0], article_revision="FA",
        stored=stored_documents(tuple(a for a, _s, _q in members)), held=held_state or held(),
    )
    return du.FamilyProbe(snapshot=snapshot, folder=job_folder(tmp_path), statuses=full)


def changed(path: pathlib.Path, members=(1,)) -> du.Changed:
    return du.Changed(file_of(path), hashing.sha256_hex(path), members)


def preview_of(probe: du.FamilyProbe, tmp_path: pathlib.Path, choices=None, **kwargs) -> du.FamilyPreview:
    return du.build_preview(probe, choices or {}, parser_registry(), tmp_path, **kwargs)


REVISED_LINES = [
    bom_line("1", "C-1", ["C1", "C2", "C3"]),
    bom_line("3", "J-1", ["J1"], mount="T"),
    bom_line("4", "U-1", ["U1"]),
    bom_line("5", "J-2", ["J2"], mount="T"),
]


def test_every_bom_diff_field(tmp_path):
    bom = write(job_folder(tmp_path) / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx", bom_text(REVISED_LINES))
    preview = preview_of(probe_with({SourceRole.BOM: changed(bom)}, tmp_path), tmp_path)

    assert preview.bom == du.BomDiff(
        lines_added=(du.BomLineKey("4", "U-1"), du.BomLineKey("5", "J-2")),
        lines_removed=(du.BomLineKey("2", "R-1"),),
        lines_changed=(du.BomLineKey("1", "C-1"),),
        designators_added=frozenset({"C3", "U1", "J2"}),
        designators_removed=frozenset({"R1"}),
        tht_items_added=1,
        tht_items_removed=0,
        cleaning=preview.bom.cleaning,
    )
    assert preview.can_apply and preview.ticked_by_default
    assert set(preview.updates) == {SourceRole.BOM}


def test_a_traveler_quantity_is_shown_not_applied(tmp_path):
    folder = job_folder(tmp_path)
    traveler = write(folder / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx",
                     traveler_text(quantity=200, process_clean="Clean"))
    probe = probe_with({SourceRole.TRAVELER: changed(traveler, members=(1, 2))}, tmp_path,
                       members=((1, "-1", 100), (2, "-2", 79)))
    preview = preview_of(probe, tmp_path)

    assert preview.traveler.quantity_now == 179
    assert preview.traveler.quantity_on_traveler == 200
    assert preview.traveler.changed_fields == (du.FieldChange("process_clean", "No Clean", "Clean"),)


def test_a_candidate_failing_its_parser_is_rejected_and_blocks_the_family(tmp_path):
    bom = write(job_folder(tmp_path) / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx",
                failing_text("NO_SHEET_HAS_HEADER"))
    preview = preview_of(probe_with({SourceRole.BOM: changed(bom)}, tmp_path), tmp_path)

    assert preview.rejected == {SourceRole.BOM: du.DocumentRejected(SourceRole.BOM, "MalformedBomError (NO_SHEET_HAS_HEADER)")}
    assert preview.updates == {}
    assert preview.bom is None
    assert not preview.can_apply


def test_an_unsupported_extension_is_rejected_by_the_gate(tmp_path):
    bom = write(job_folder(tmp_path) / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.txt", bom_text(REVISED_LINES))
    preview = preview_of(probe_with({SourceRole.BOM: changed(bom)}, tmp_path), tmp_path)
    assert preview.rejected == {SourceRole.BOM: du.DocumentRejected(SourceRole.BOM, "UNSUPPORTED_EXTENSION")}


def test_identity_mismatch_on_part_number_and_on_the_order(tmp_path):
    folder = job_folder(tmp_path)
    eco = write(folder / f"{PART_NUMBER} ECO_11-04-26.docx", eco_text(part_number="B900002"))
    traveler = write(folder / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx", traveler_text(sales_order_number="SO-9999"))
    probe = probe_with({SourceRole.NOTES: changed(eco), SourceRole.TRAVELER: changed(traveler)}, tmp_path)
    preview = preview_of(probe, tmp_path)

    assert preview.rejected == {
        SourceRole.NOTES: du.IdentityMismatch(SourceRole.NOTES, "part_number", PART_NUMBER, "B900002"),
        SourceRole.TRAVELER: du.IdentityMismatch(SourceRole.TRAVELER, "sales_order_number", WORK_ORDER, "SO-9999"),
    }
    assert not preview.can_apply


def test_a_traveler_without_a_quantity_is_rejected(tmp_path):
    traveler = write(job_folder(tmp_path) / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx", traveler_text(quantity=None))
    preview = preview_of(probe_with({SourceRole.TRAVELER: changed(traveler)}, tmp_path), tmp_path)
    assert preview.rejected == {SourceRole.TRAVELER: du.DocumentRejected(SourceRole.TRAVELER, "QUANTITY_INVALID")}


def test_a_role_awaiting_a_choice_has_no_diff_until_picked(tmp_path):
    folder = job_folder(tmp_path)
    first = write(folder / f"{PART_NUMBER} AUDIT BOM.xlsx", bom_text(REVISED_LINES))
    second = write(folder / f"{PART_NUMBER} AUDIT BOM copy.xlsx", bom_text(REVISED_LINES[:1]))
    probe = probe_with({SourceRole.BOM: du.NeedsChoice((file_of(first), file_of(second)))}, tmp_path)

    unpicked = preview_of(probe, tmp_path)
    assert unpicked.awaiting_choice == (SourceRole.BOM,)
    assert unpicked.bom is None and not unpicked.can_apply

    picked = preview_of(probe, tmp_path, choices={SourceRole.BOM: second})
    assert picked.awaiting_choice == ()
    assert picked.updates[SourceRole.BOM].candidate.path == second
    assert picked.bom.lines_removed == (du.BomLineKey("2", "R-1"), du.BomLineKey("3", "J-1"))
    assert picked.can_apply


def test_a_pick_that_is_not_a_candidate_is_refused(tmp_path):
    folder = job_folder(tmp_path)
    first = write(folder / f"{PART_NUMBER} AUDIT BOM.xlsx", bom_text(REVISED_LINES))
    second = write(folder / f"{PART_NUMBER} AUDIT BOM copy.xlsx", bom_text(REVISED_LINES))
    elsewhere = write(tmp_path / "elsewhere" / f"{PART_NUMBER} AUDIT BOM.xlsx", bom_text(REVISED_LINES))
    probe = probe_with({SourceRole.BOM: du.NeedsChoice((file_of(first), file_of(second)))}, tmp_path)

    preview = preview_of(probe, tmp_path, choices={SourceRole.BOM: elsewhere})
    assert preview.rejected == {SourceRole.BOM: du.DocumentRejected(SourceRole.BOM, "PICK_NOT_A_CANDIDATE")}


def test_an_older_version_is_diffed_but_not_ticked(tmp_path):
    bom = write(job_folder(tmp_path) / f"{PART_NUMBER} AUDIT BOM update 10-01-2026.xlsx", bom_text(REVISED_LINES))
    older = du.OlderOnShare(file_of(bom), hashing.sha256_hex(bom), (1,), "older")
    preview = preview_of(probe_with({SourceRole.BOM: older}, tmp_path), tmp_path)
    assert preview.bom is not None
    assert preview.can_apply and not preview.ticked_by_default


def test_ops_per_board_is_marked_for_review_only_when_the_bom_changes_and_a_figure_is_entered(tmp_path):
    folder = job_folder(tmp_path)
    bom = write(folder / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx", bom_text(REVISED_LINES))
    eco = write(folder / f"{PART_NUMBER} ECO_11-04-26.docx", eco_text())

    with_figure = held(ops_per_board_min=2.5)
    assert preview_of(probe_with({SourceRole.BOM: changed(bom)}, tmp_path, with_figure), tmp_path).ops_per_board_review
    assert not preview_of(probe_with({SourceRole.NOTES: changed(eco)}, tmp_path, with_figure), tmp_path).ops_per_board_review
    assert not preview_of(probe_with({SourceRole.BOM: changed(bom)}, tmp_path), tmp_path).ops_per_board_review


def test_the_requirements_previewer_is_optional_and_best_effort(tmp_path):
    eco = write(job_folder(tmp_path) / f"{PART_NUMBER} ECO_11-04-26.docx", eco_text())
    probe = probe_with({SourceRole.NOTES: changed(eco)}, tmp_path)
    seen = []

    def previewer(probe_arg, paths):
        seen.append(dict(paths))
        return du.RequirementDiff(before=None, after="XR", tick_will_need_recheck=True)

    assert preview_of(probe, tmp_path, requirements=previewer).requirements.tick_will_need_recheck
    assert seen == [{SourceRole.NOTES: eco}]

    def broken(probe_arg, paths):
        raise RuntimeError("scanner unavailable")

    assert preview_of(probe, tmp_path, requirements=broken).requirements is None
    assert preview_of(probe, tmp_path).requirements is None


def test_nothing_is_copied_or_written(tmp_path):
    folder = job_folder(tmp_path)
    bom = write(folder / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx", bom_text(REVISED_LINES))
    traveler = write(folder / f"{PART_NUMBER} MFG TRAVELER_11-04-26.xlsx", traveler_text())

    def tree() -> dict:
        return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in tmp_path.rglob("*")}

    before = tree()
    preview_of(probe_with({SourceRole.BOM: changed(bom), SourceRole.TRAVELER: changed(traveler)}, tmp_path), tmp_path)
    assert tree() == before
