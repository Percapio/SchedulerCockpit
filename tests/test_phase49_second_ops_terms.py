"""Phase 49 section 8.2 — shipped 2nd OPS vocabulary and its migration."""

import pytest
from PyQt6.QtCore import QSettings

from cockpit.services.second_ops import (
    DEFAULT_SECOND_OPS_TERMS,
    DEFAULT_TERMS_GENERATION,
    SECOND_OPS_TERMS_GENERATION_KEY,
    SECOND_OPS_TERMS_KEY,
    SHIPPED_TERMS_BY_GENERATION,
    SecondOpsSettingsController,
    merge_shipped_terms,
)


@pytest.fixture
def controller(tmp_path):
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    return SecondOpsSettingsController(settings), settings


# --- the shipped vocabulary -------------------------------------------------

def test_defaults_are_the_generations_concatenated_in_order():
    expected = tuple(
        term
        for generation in sorted(SHIPPED_TERMS_BY_GENERATION)
        for term in SHIPPED_TERMS_BY_GENERATION[generation]
    )
    assert DEFAULT_SECOND_OPS_TERMS == expected


def test_defaults_hold_twenty_six_terms_with_no_duplicate_at_any_casing():
    assert len(DEFAULT_SECOND_OPS_TERMS) == 26
    lowered = [t.lower() for t in DEFAULT_SECOND_OPS_TERMS]
    assert len(set(lowered)) == len(lowered)


def test_term_block_plug_is_absent_because_plug_and_block_subsume_it():
    lowered = {t.lower() for t in DEFAULT_SECOND_OPS_TERMS}
    assert "term block plug" not in lowered
    assert {"plug", "block"} <= lowered


# --- merge_shipped_terms ----------------------------------------------------

def test_generation_one_store_gains_exactly_the_generation_two_terms():
    stored = SHIPPED_TERMS_BY_GENERATION[1]
    merged = merge_shipped_terms(stored, 1)
    assert merged[: len(stored)] == stored
    assert merged[len(stored):] == SHIPPED_TERMS_BY_GENERATION[2]


def test_a_deleted_term_stays_deleted():
    stored = tuple(t for t in SHIPPED_TERMS_BY_GENERATION[1] if t != "Nut")
    merged = merge_shipped_terms(stored, 1)
    assert "nut" not in {t.lower() for t in merged}


def test_existing_term_keeps_its_own_capitalisation():
    merged = merge_shipped_terms(("washer",), 1)
    assert "washer" in merged
    assert "WASHER" not in merged
    assert sum(1 for t in merged if t.lower() == "washer") == 1


def test_existing_term_at_shipped_capitalisation_is_not_duplicated():
    merged = merge_shipped_terms(("WASHER",), 1)
    assert sum(1 for t in merged if t.lower() == "washer") == 1


def test_store_already_current_gains_nothing():
    stored = ("Custom",)
    assert merge_shipped_terms(stored, DEFAULT_TERMS_GENERATION) == stored


def test_operator_terms_keep_their_order_and_precede_the_appended_ones():
    stored = ("Zeta", "Alpha")
    merged = merge_shipped_terms(stored, 1)
    assert merged[:2] == ("Zeta", "Alpha")


# --- migrate_shipped_terms --------------------------------------------------

def test_absent_key_writes_nothing(controller):
    ctrl, settings = controller
    outcome = ctrl.migrate_shipped_terms()
    assert outcome.added == ()
    assert not settings.contains(SECOND_OPS_TERMS_KEY)
    assert not settings.contains(SECOND_OPS_TERMS_GENERATION_KEY)
    assert ctrl.terms() == DEFAULT_SECOND_OPS_TERMS


def test_customised_store_without_a_marker_is_migrated_from_generation_one(controller):
    ctrl, settings = controller
    settings.setValue(SECOND_OPS_TERMS_KEY, "Fuse, Shunt")

    outcome = ctrl.migrate_shipped_terms()

    assert outcome.added == SHIPPED_TERMS_BY_GENERATION[2]
    assert outcome.generation_advanced_to == DEFAULT_TERMS_GENERATION
    assert ctrl.terms()[:2] == ("Fuse", "Shunt")
    assert settings.value(SECOND_OPS_TERMS_GENERATION_KEY, type=int) == DEFAULT_TERMS_GENERATION


def test_migration_is_idempotent(controller):
    ctrl, settings = controller
    settings.setValue(SECOND_OPS_TERMS_KEY, "Fuse")

    first = ctrl.migrate_shipped_terms()
    after_first = ctrl.terms()
    second = ctrl.migrate_shipped_terms()

    assert first.added
    assert second.added == ()
    assert ctrl.terms() == after_first


def test_edit_in_settings_stamps_the_generation(controller):
    """Finding 2: without the stamp the next launch re-appends everything."""
    ctrl, settings = controller
    ctrl.set_terms_from_text("Fuse, Nut")
    assert settings.value(SECOND_OPS_TERMS_GENERATION_KEY, type=int) == DEFAULT_TERMS_GENERATION


def test_never_customised_then_edited_then_relaunched_keeps_deletions(controller):
    """The full sequence finding 2 names.

    A store that never held a terms key resolves to the current defaults. The
    operator edits the list, deleting one shipped term. On the next launch the
    migration must not bring it back.
    """
    ctrl, settings = controller

    assert ctrl.migrate_shipped_terms().added == ()

    kept = tuple(t for t in DEFAULT_SECOND_OPS_TERMS if t != "LOCTITE")
    ctrl.set_terms_from_text(", ".join(kept))

    outcome = ctrl.migrate_shipped_terms()

    assert outcome.added == ()
    assert "loctite" not in {t.lower() for t in ctrl.terms()}


def test_unchanged_text_still_stamps_a_missing_marker(controller):
    ctrl, settings = controller
    settings.setValue(SECOND_OPS_TERMS_KEY, "Fuse, Nut")

    ctrl.set_terms_from_text("Fuse, Nut")

    assert settings.value(SECOND_OPS_TERMS_GENERATION_KEY, type=int) == DEFAULT_TERMS_GENERATION


def test_restore_defaults_removes_both_keys(controller):
    ctrl, settings = controller
    ctrl.set_terms_from_text("Fuse")
    assert settings.contains(SECOND_OPS_TERMS_GENERATION_KEY)

    ctrl.restore_defaults()

    assert not settings.contains(SECOND_OPS_TERMS_KEY)
    assert not settings.contains(SECOND_OPS_TERMS_GENERATION_KEY)
    assert ctrl.terms() == DEFAULT_SECOND_OPS_TERMS


def test_unwritable_store_keeps_a_coherent_older_generation(controller, monkeypatch):
    ctrl, settings = controller
    settings.setValue(SECOND_OPS_TERMS_KEY, "Fuse")

    def boom(*args, **kwargs):
        raise OSError("settings store is read-only")

    monkeypatch.setattr(ctrl._settings, "setValue", boom)

    outcome = ctrl.migrate_shipped_terms()

    assert outcome.added == ()
    assert outcome.generation_advanced_to == 1
    assert ctrl.terms() == ("Fuse",)
