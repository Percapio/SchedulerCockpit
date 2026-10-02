"""Phase 52 section 9 — local corpus regression against the AOI programmer's files.

The corpus holds customer data and is gitignored; these tests run only on a
machine that has it, and are skipped everywhere else.
"""

import pathlib

import pytest

from cockpit.services.aoi.export import AoiExportPlan, AoiExportRequest, prepare_aoi_export
from cockpit.services.aoi.mirtec import compare_aoi, render_mirtec
from cockpit.services.aoi.types import BoardSide

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "backend" / "data" / "data"
SAMPLE_JOBS = ("B142xxx/B1425xx/B142501", "B142xxx/B1422xx/B142286")


def _job_dirs():
    return [CORPUS / rel for rel in SAMPLE_JOBS if (CORPUS / rel / "aoi").is_dir()]


pytestmark = pytest.mark.skipif(not _job_dirs(), reason="AOI corpus samples not present on this machine")


def _plan(job_dir: pathlib.Path) -> AoiExportPlan:
    plan = prepare_aoi_export(AoiExportRequest(
        job_number=job_dir.name,
        source_root=CORPUS,
        stored_bom=next(job_dir.glob("bom/**/*.xlsx")),
        program_root=job_dir / "program",
        xy_path=next(job_dir.glob("xy/**/*XYRS*.csv")),
    ))
    assert isinstance(plan, AoiExportPlan)
    return plan


@pytest.mark.parametrize("job_dir", _job_dirs(), ids=lambda p: p.name)
def test_every_sample_line_is_reproduced(job_dir):
    plan = _plan(job_dir)
    checked = 0
    for document in plan.documents():
        sample = job_dir / "aoi" / f"{document.side.value}-{job_dir.name}-XY.txt"
        if not sample.exists():
            continue
        first = 1 if document.side is BoardSide.TOP else plan.default_bottom_start()
        generated = render_mirtec(document, first).splitlines()
        expected = sample.read_text().splitlines()
        assert set(generated) == set(expected)
        assert len(generated) == len(expected)
        assert compare_aoi(document, first, sample).is_identical
        checked += 1
    assert checked >= 1


def test_side_without_program_is_not_exported_by_default():
    job_dir = CORPUS / SAMPLE_JOBS[0]
    if not (job_dir / "aoi").is_dir():
        pytest.skip("B142501 sample absent")
    assert _plan(job_dir).default_sides() == (BoardSide.TOP,)
