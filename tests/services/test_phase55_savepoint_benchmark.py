"""Phase 55 section 6.3: the savepoint budget, measured on demand.

The load is the largest corpus Audit BOM's shape (4,159 designators on 243
lines, one of them through-hole) applied to a four-sibling family. Run with
COCKPIT_PHASE55_BENCHMARK=1; the shipping gate is the same measurement on the
shop PC.
"""

import os
import time

import pytest

from cockpit.services import document_updates as du
from tests.services.phase55_support import PART_NUMBER, bom_line, bom_text, job_folder, make_shop, write, \
    write_share_documents

BUDGET_SECONDS = 0.250
DESIGNATORS = 4_159
LINES = 243
SIBLINGS = 4

pytestmark = pytest.mark.skipif(
    os.environ.get("COCKPIT_PHASE55_BENCHMARK") != "1",
    reason="on demand: set COCKPIT_PHASE55_BENCHMARK=1",
)


def corpus_shaped_lines(prefix: str) -> list[dict]:
    per_line, extra = divmod(DESIGNATORS, LINES)
    lines, next_designator = [], 0
    for line in range(LINES):
        count = per_line + (1 if line < extra else 0)
        ref_des = [f"{prefix}{next_designator + n}" for n in range(count)]
        next_designator += count
        lines.append(bom_line(str(line + 1), f"MPN-{line}", ref_des, mount="T" if line == 0 else "S"))
    return lines


def test_the_largest_bom_applied_to_four_siblings_fits_the_budget(tmp_path, monkeypatch):
    shop = make_shop(tmp_path, monkeypatch)
    documents = write_share_documents(job_folder(shop.source_root), corpus_shaped_lines("C"))
    first = shop.ingest(documents)
    for n in range(SIBLINGS - 1):
        shop.split(first, f"-{n + 2}", 1)

    write(job_folder(shop.source_root) / f"{PART_NUMBER} AUDIT BOM update 11-04-2026.xlsx",
          bom_text(corpus_shaped_lines("R")))
    plan = shop.prepare(shop.check())
    assert isinstance(plan, du.FamilyUpdatePlan)

    conn = shop.repositories.conn
    marks: dict[str, float] = {}
    shipped_execute = conn.execute

    class TimedConnection:
        def __getattr__(self, name):
            return getattr(conn, name)

        def execute(self, sql, *args):
            if sql.startswith("SAVEPOINT update_family"):
                marks["start"] = time.perf_counter()
            result = shipped_execute(sql, *args)
            if sql.startswith("RELEASE SAVEPOINT update_family"):
                marks["end"] = time.perf_counter()
            return result

    timed = du.FamilyUpdateRepositories(**{**shop.repositories.__dict__, "conn": TimedConnection()})
    outcome = du.apply_family_update(plan, timed, shop.runtime_calc)

    assert isinstance(outcome, du.FamilyUpdated)
    held = marks["end"] - marks["start"]
    print(f"\nsavepoint held the UI thread for {held * 1000:.1f} ms "
          f"({DESIGNATORS} designators x {SIBLINGS} siblings)")
    assert held <= BUDGET_SECONDS
