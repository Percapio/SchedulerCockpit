"""Phase 55 sections 3.3-3.5: decide_role's check order and probe_family."""

import pathlib
from types import SimpleNamespace

import pytest

from cockpit.ingestion.errors import JobDirectoryEscapesRoot
from cockpit.ingestion.roles import SourceRole
from cockpit.ingestion.version_tags import version_tag
from cockpit.services import document_updates as du
from tests.services.phase55_support import PART_NUMBER, WORK_ORDER, job_folder, write

SEARCHED = pathlib.Path("share") / "job"
OLD_HASH = "a" * 64
NEW_HASH = "b" * 64


def candidate(name: str, size: int = 10, mtime: int = 1_000) -> du.CandidateFile:
    return du.CandidateFile(path=SEARCHED / name, name=name, size=size, modified_at_ns=mtime, tag=version_tag(name))


def stored(name: str, role: SourceRole = SourceRole.BOM, file_hash: str = OLD_HASH, size: int = 10,
           mtime: int = 1_000) -> du.StoredDocument:
    return du.StoredDocument(role=role, source_file_id=1, original_filename=name,
                             stored_path=pathlib.Path("storage") / name, file_hash=file_hash,
                             size=size, modified_at_ns=mtime)


class Reader:
    def __init__(self, result):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, file: du.CandidateFile):
        self.calls.append(file.name)
        return self.result


def decide(candidates, reference, member_hashes, reader, role=SourceRole.BOM):
    return du.decide_role(role, candidates, reference, member_hashes, SEARCHED, reader)


# ---------- decide_role ----------

def test_no_candidates_is_missing_without_a_read():
    reader = Reader(NEW_HASH)
    assert decide([], stored("X AUDIT BOM.xlsx"), {1: OLD_HASH}, reader) == du.Missing(SEARCHED)
    assert reader.calls == []


def test_unrankable_candidates_need_a_choice_before_anything_is_read():
    reader = Reader(NEW_HASH)
    both = [candidate("X AUDIT BOM.xlsx"), candidate("X AUDIT BOM copy.xlsx")]
    status = decide(both, stored("X AUDIT BOM.xlsx"), {1: OLD_HASH}, reader)
    assert isinstance(status, du.NeedsChoice)
    assert [c.name for c in status.candidates] == ["X AUDIT BOM copy.xlsx", "X AUDIT BOM.xlsx"]
    assert reader.calls == []


def test_unchanged_by_name_size_and_time_without_a_read():
    reader = Reader(NEW_HASH)
    status = decide([candidate("X AUDIT BOM.xlsx")], stored("X AUDIT BOM.xlsx"), {1: OLD_HASH, 2: OLD_HASH}, reader)
    assert status == du.Unchanged()
    assert reader.calls == []


def test_the_fast_path_is_skipped_for_a_split_family_and_names_the_lagging_member():
    reader = Reader(NEW_HASH)
    status = decide([candidate("X AUDIT BOM.xlsx")], stored("X AUDIT BOM.xlsx"), {1: OLD_HASH, 2: NEW_HASH}, reader)
    assert status == du.Changed(candidate("X AUDIT BOM.xlsx"), NEW_HASH, (1,))
    assert reader.calls == ["X AUDIT BOM.xlsx"]


def test_the_fast_path_wins_over_an_older_modification_time():
    reader = Reader(NEW_HASH)
    reference = stored("X AUDIT BOM.xlsx", mtime=5_000)
    status = decide([candidate("X AUDIT BOM.xlsx", mtime=5_000)], reference, {1: OLD_HASH}, reader)
    assert status == du.Unchanged()


def test_unchanged_by_hash_after_an_identical_re_save():
    reader = Reader(OLD_HASH)
    status = decide([candidate("X AUDIT BOM.xlsx", mtime=9_000)], stored("X AUDIT BOM.xlsx"), {1: OLD_HASH}, reader)
    assert status == du.Unchanged()
    assert reader.calls == ["X AUDIT BOM.xlsx"]


def test_older_on_share_by_name_date():
    reference = stored("X AUDIT BOM update 10-29-2026.xlsx")
    status = decide([candidate("X AUDIT BOM update 10-15-2026.xlsx")], reference, {1: OLD_HASH}, Reader(NEW_HASH))
    assert isinstance(status, du.OlderOnShare)
    assert "10-29-2026" in status.reason and "10-15-2026" in status.reason
    assert status.members_to_update == (1,)


def test_older_on_share_when_the_stored_name_is_dated_and_the_share_is_not():
    reference = stored("X AUDIT BOM update 10-29-2026.xlsx")
    status = decide([candidate("X AUDIT BOM.xlsx")], reference, {1: OLD_HASH}, Reader(NEW_HASH))
    assert isinstance(status, du.OlderOnShare)
    assert "undated" in status.reason


def test_older_on_share_by_modification_time_when_neither_name_is_dated():
    reference = stored("X AUDIT BOM.xlsx", mtime=5_000)
    status = decide([candidate("X AUDIT BOM.xlsx", size=11, mtime=4_000)], reference, {1: OLD_HASH}, Reader(NEW_HASH))
    assert isinstance(status, du.OlderOnShare)


def test_a_newer_dated_candidate_is_changed_for_every_member():
    reference = stored("X AUDIT BOM update 10-15-2026.xlsx")
    status = decide([candidate("X AUDIT BOM update 10-29-2026.xlsx")], reference, {1: OLD_HASH, 2: OLD_HASH},
                    Reader(NEW_HASH))
    assert status == du.Changed(candidate("X AUDIT BOM update 10-29-2026.xlsx"), NEW_HASH, (1, 2))


def test_a_share_read_failure_is_returned_not_raised():
    failure = du.ShareReadFailure(SEARCHED / "X AUDIT BOM.xlsx", "PermissionError (5)")
    status = decide([candidate("X AUDIT BOM.xlsx", mtime=9_000)], stored("X AUDIT BOM.xlsx"), {1: OLD_HASH},
                    Reader(failure))
    assert status == failure


def test_undated_travelers_are_ranked_by_revision():
    reference = stored("X MFG TRAVELER rev A.xlsx", role=SourceRole.TRAVELER)
    both = [candidate("X MFG TRAVELER rev A.xlsx"), candidate("X MFG TRAVELER rev B.xlsx")]
    status = decide(both, reference, {1: OLD_HASH}, Reader(NEW_HASH), role=SourceRole.TRAVELER)
    assert isinstance(status, du.Changed)
    assert status.candidate.name == "X MFG TRAVELER rev B.xlsx"


# ---------- probe_family ----------

def held_state() -> du.HeldState:
    return du.HeldState(components=(), tht_items=(), traveler_metadata={}, ops_per_board_min=None)


def snapshot_of(folder_files: dict[SourceRole, pathlib.Path], members=((1, ""),), article="FA",
                omit: tuple[tuple[int, SourceRole], ...] = ()) -> du.FamilySnapshot:
    """A family whose every member holds exactly the files given, stat for stat."""
    stored_by_member = {}
    for audit_id, _suffix in members:
        documents = {}
        for role, path in folder_files.items():
            if (audit_id, role) in omit:
                continue
            stat = path.stat()
            documents[role] = du.StoredDocument(
                role=role, source_file_id=audit_id * 10, original_filename=path.name,
                stored_path=path, file_hash=OLD_HASH, size=stat.st_size, modified_at_ns=stat.st_mtime_ns,
            )
        stored_by_member[audit_id] = documents
    return du.FamilySnapshot(
        key=du.FamilyKey(PART_NUMBER, WORK_ORDER),
        members=tuple(du.FamilyMember(a, s, 10, frozenset()) for a, s in members),
        reference_member=members[0][0], article_revision=article, stored=stored_by_member, held=held_state(),
    )


def share_set(folder: pathlib.Path) -> dict[SourceRole, pathlib.Path]:
    return {
        SourceRole.BOM: write(folder / f"{PART_NUMBER} AUDIT BOM.xlsx", "bom"),
        SourceRole.NOTES: write(folder / f"{PART_NUMBER} ECO.docx", "eco"),
        SourceRole.TRAVELER: write(folder / f"{PART_NUMBER} MFG TRAVELER.xlsx", "traveler"),
    }


def never_read(file: du.CandidateFile):
    raise AssertionError(f"read {file.name}")


def test_an_unchanged_family_is_probed_from_its_root_without_a_read(tmp_path):
    folder = job_folder(tmp_path)
    probe = du.probe_family(snapshot_of(share_set(folder)), tmp_path, never_read)
    assert isinstance(probe, du.FamilyProbe)
    assert probe.folder == folder
    assert set(probe.statuses.values()) == {du.Unchanged()}


def test_an_article_is_probed_in_its_ordinal_folder(tmp_path):
    folder = job_folder(tmp_path)
    share_set(folder)
    article_files = share_set(folder / "2nd Article")
    probe = du.probe_family(snapshot_of(article_files, article="2nd"), tmp_path, never_read)
    assert probe.folder == folder / "2nd Article"
    assert set(probe.statuses.values()) == {du.Unchanged()}


def test_two_folders_declaring_the_ordinal_are_ambiguous(tmp_path):
    folder = job_folder(tmp_path)
    files = share_set(folder / "2nd")
    share_set(folder / "2ND_REV_B")
    outcome = du.probe_family(snapshot_of(files, article="2nd"), tmp_path, never_read)
    assert isinstance(outcome, du.ArticleFolderAmbiguous)
    assert {p.name for p in outcome.candidates} == {"2nd", "2ND_REV_B"}


def test_a_missing_folder_makes_every_role_missing(tmp_path):
    files = share_set(tmp_path / "elsewhere")
    probe = du.probe_family(snapshot_of(files), tmp_path / "share", never_read)
    assert all(isinstance(status, du.Missing) for status in probe.statuses.values())
    assert probe.folder is None

    job = job_folder(tmp_path / "share")
    probe = du.probe_family(snapshot_of(files, article="3rd"), tmp_path / "share", never_read)
    assert set(probe.statuses.values()) == {du.Missing(job)}


def test_old_subfolders_are_never_entered_and_lock_files_are_ignored(tmp_path):
    folder = job_folder(tmp_path)
    files = share_set(folder)
    write(folder / "OLD" / f"{PART_NUMBER} AUDIT BOM update 12-01-2026.xlsx", "newer but archived")
    write(folder / f"~${PART_NUMBER} AUDIT BOM.xlsx", "lock")
    probe = du.probe_family(snapshot_of(files), tmp_path, never_read)
    assert probe.statuses[SourceRole.BOM] == du.Unchanged()


def test_a_role_any_member_lacks_is_not_stored(tmp_path):
    folder = job_folder(tmp_path)
    snapshot = snapshot_of(share_set(folder), members=((1, ""), (2, "-2")), omit=((2, SourceRole.TRAVELER),))
    probe = du.probe_family(snapshot, tmp_path, never_read)
    assert probe.statuses[SourceRole.TRAVELER] == du.NotStored()
    assert probe.statuses[SourceRole.BOM] == du.Unchanged()


def test_an_unreadable_candidate_fails_its_family_only(tmp_path):
    folder = job_folder(tmp_path)
    files = share_set(folder)
    snapshot = snapshot_of(files)
    write(files[SourceRole.BOM], "revised in place")
    failure = du.ShareReadFailure(files[SourceRole.BOM], "PermissionError (5)")
    outcome = du.probe_family(snapshot, tmp_path, lambda file: failure)
    assert outcome == du.CandidateUnreadable(files[SourceRole.BOM], "PermissionError (5)")


def test_a_changed_candidate_is_hashed_and_reported(tmp_path):
    folder = job_folder(tmp_path)
    files = share_set(folder)
    snapshot = snapshot_of(files)
    write(files[SourceRole.BOM], "revised in place, longer than before")
    probe = du.probe_family(snapshot, tmp_path)
    status = probe.statuses[SourceRole.BOM]
    assert isinstance(status, du.Changed)
    assert status.members_to_update == (1,)


def test_a_candidate_outside_the_root_is_never_read(tmp_path, monkeypatch):
    folder = job_folder(tmp_path)
    files = share_set(folder)
    snapshot = snapshot_of(files)
    write(files[SourceRole.BOM], "revised in place, longer than before")
    shipped = du.assert_within_root

    def refuse_the_bom(candidate_path, root):
        if candidate_path == files[SourceRole.BOM]:
            raise JobDirectoryEscapesRoot(PART_NUMBER, candidate_path, root)
        shipped(candidate_path, root)

    monkeypatch.setattr(du, "assert_within_root", refuse_the_bom)
    outcome = du.probe_family(snapshot, tmp_path, never_read)
    assert outcome == du.EscapesRoot(files[SourceRole.BOM])


def test_a_listing_that_fails_is_share_unreachable(tmp_path, monkeypatch):
    files = share_set(job_folder(tmp_path))

    def unreachable(job_number, root):
        raise OSError(64, "The specified network name is no longer available")

    monkeypatch.setattr(du, "resolve_job_directory", unreachable)
    outcome = du.probe_family(snapshot_of(files), tmp_path, never_read)
    assert isinstance(outcome, du.ShareUnreachable)
    assert "network name" not in outcome.cause


@pytest.mark.parametrize("family, expected", [
    ([(5, "-2"), (3, "-1")], 3),
    ([(9, ""), (3, "-1")], 9),
    ([(4, "-1"), (9, ""), (2, "-3")], 9),
])
def test_the_reference_member_is_the_unsuffixed_one_else_the_lowest_id(family, expected):
    audits = [SimpleNamespace(id=audit_id, split_suffix=suffix) for audit_id, suffix in family]
    assert du.choose_reference_member(audits) == expected
    assert du.choose_reference_member(list(reversed(audits))) == expected
