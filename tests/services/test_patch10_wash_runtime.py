"""Patch 10 section 9.3 — the multiplier no longer fires on a No Clean build.

test_runtime_calc constructs RuntimeInputs by hand and so is insulated from the
predicate. These exercise the predicate and the arithmetic together, through
the stored boolean, which is where the defect actually lived.
"""

import json
import sqlite3

import pytest

from cockpit.persistence.schema import migrate_to_v10
from cockpit.persistence.traveler_flags import is_clean_process
from cockpit.services.runtime_calc import (
    RuntimeConstants,
    RuntimeInputs,
    compute_ops,
    compute_tht,
)


def _inputs(is_clean: bool) -> RuntimeInputs:
    return RuntimeInputs(
        smt_placements=0,
        smt_unique_mpns=0,
        tht_placements=100,
        quantity=10,
        sides=1,
        is_class_3=False,
        is_clean_process=is_clean,
        ops_per_board_min=6.0,
    )


def test_a_no_clean_traveler_carries_no_clean_factor_into_tht():
    constants = RuntimeConstants()
    metadata = {"process_clean": "No Clean"}

    tht = compute_tht(_inputs(is_clean_process(metadata)), constants)
    unmultiplied = compute_tht(_inputs(False), constants)

    assert tht == unmultiplied


def test_a_clean_traveler_still_carries_the_clean_factor_into_tht():
    constants = RuntimeConstants()
    metadata = {"process_clean": "Clean"}

    tht = compute_tht(_inputs(is_clean_process(metadata)), constants)
    unmultiplied = compute_tht(_inputs(False), constants)

    assert tht == pytest.approx(unmultiplied * constants.clean_process_multiplier_tht)


def test_the_no_clean_correction_lowers_tht_by_the_multiplier():
    """The direction stated in Patch 10 section 3.2: the figure goes down."""
    constants = RuntimeConstants()

    before_patch = compute_tht(_inputs(True), constants)
    after_patch = compute_tht(_inputs(is_clean_process({"process_clean": "No Clean"})), constants)

    assert after_patch < before_patch
    assert before_patch == pytest.approx(after_patch * constants.clean_process_multiplier_tht)


def test_ops_follows_the_same_predicate():
    constants = RuntimeConstants(clean_process_multiplier_ops=2.0)

    clean = compute_ops(_inputs(is_clean_process({"process_clean": "Clean"})), constants)
    no_clean = compute_ops(_inputs(is_clean_process({"process_clean": "No Clean"})), constants)

    assert clean is not None and no_clean is not None
    assert clean > no_clean


# --- the v10 backfill, Patch 10 sections 5 and 6.3 --------------------------

_V9_MINIMAL_SCHEMA = """
CREATE TABLE schema_version (
    singleton_guard INTEGER PRIMARY KEY CHECK (singleton_guard = 1),
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE active_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_number TEXT NOT NULL,
    traveler_metadata TEXT
);
"""


def _v9_database(travelers: list[dict | None]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(_V9_MINIMAL_SCHEMA)
    conn.execute(
        "INSERT INTO schema_version (singleton_guard, version, applied_at) VALUES (1, 9, '2026-01-01T00:00:00+00:00')"
    )
    for index, traveler in enumerate(travelers):
        conn.execute(
            "INSERT INTO active_audits (part_number, traveler_metadata) VALUES (?, ?)",
            (f"B{index:06d}", json.dumps(traveler) if traveler is not None else None),
        )
    conn.commit()
    return conn


def test_v10_backfills_a_no_clean_traveler_as_not_a_clean_process():
    conn = _v9_database([{"process_clean": "No Clean"}, {"process_clean": "Clean"}])

    migrate_to_v10(conn)

    rows = conn.execute(
        "SELECT part_number, is_clean_process FROM active_audits ORDER BY id"
    ).fetchall()
    assert [r["is_clean_process"] for r in rows] == [0, 1]


def test_v10_aggregates_off_template_travelers_into_one_warning(caplog):
    """Section 6.3: a bulk path emits a count, not a line per row."""
    import logging

    conn = _v9_database(
        [
            {"process_clean": "NC"},
            {"process_clean": "NC"},
            {"process_clean": "WASH"},
            {"process_clean": "Clean"},
            {},
        ]
    )

    with caplog.at_level(logging.WARNING, logger="cockpit.persistence.schema"):
        migrate_to_v10(conn)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "3 audit(s)" in message
    assert "NC" in message and "WASH" in message


def test_v10_says_nothing_when_every_traveler_is_on_template(caplog):
    import logging

    conn = _v9_database([{"process_clean": "Clean"}, {"process_clean": "No Clean"}, {}])

    with caplog.at_level(logging.WARNING, logger="cockpit.persistence.schema"):
        migrate_to_v10(conn)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []
