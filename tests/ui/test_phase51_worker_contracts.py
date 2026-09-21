"""Phase 51 — the worker call sites MainWindow actually uses.

These exist because a phase shipped with 821 green tests and a broken
application: MainWindow was updated to pass `scope=` and `plan=` to workers
whose signatures had not been changed, and nothing in the suite constructed
either worker the way MainWindow does. Every test here binds the exact keyword
set the call site uses, so a signature drifting away from its caller fails here
rather than in the operator's error dialog.
"""

import ast
import inspect
import pathlib

import pytest

from cockpit.ingestion.locator import IngestionScope
from cockpit.ui.job_fetch_worker import DrawingFetchWorker, JobFetchWorker
from cockpit.ui.workers import IngestionWorker

MAIN_WINDOW = pathlib.Path("cockpit/ui/main_window.py")


def keywords_at_call_site(class_name: str) -> set[str]:
    """The keyword names MainWindow passes when constructing `class_name`."""
    tree = ast.parse(MAIN_WINDOW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == class_name
        ):
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError(f"MainWindow never constructs {class_name}")


@pytest.mark.parametrize(
    "worker,positional",
    [
        (JobFetchWorker, ("B123456", pathlib.Path("."))),
        (DrawingFetchWorker, ("B123456", pathlib.Path("."))),
        (IngestionWorker, (object(), [])),
    ],
)
def test_main_windows_keywords_bind_to_the_signature(worker, positional):
    """The check that would have caught the shipped defect."""
    keywords = keywords_at_call_site(worker.__name__)
    signature = inspect.signature(worker.__init__)
    # Raises TypeError if MainWindow passes a keyword the worker cannot accept.
    signature.bind(None, *positional, **{k: None for k in keywords})


def test_job_fetch_worker_takes_a_scope_and_a_folder(tmp_path):
    worker = JobFetchWorker(
        "B123456", tmp_path,
        scope=IngestionScope.ARTICLE_SUBFOLDER,
        article_folder=tmp_path / "2nd",
    )
    assert worker.scope is IngestionScope.ARTICLE_SUBFOLDER
    assert worker.article_folder == tmp_path / "2nd"


def test_job_fetch_worker_defaults_to_root_scope(tmp_path, monkeypatch):
    seen = {}

    def fake_locate(job_number, source_root, scope, article_folder):
        seen.update(scope=scope, article_folder=article_folder)
        raise RuntimeError("stop here")

    monkeypatch.setattr("cockpit.ingestion.locator.locate", fake_locate)
    monkeypatch.setattr("cockpit.ui.job_fetch_worker.locate", fake_locate)

    JobFetchWorker("B123456", tmp_path).run()

    assert seen["scope"] is IngestionScope.ROOT
    assert seen["article_folder"] is None


def test_job_fetch_worker_forwards_the_scope(tmp_path, monkeypatch):
    seen = {}

    def fake_locate(job_number, source_root, scope, article_folder):
        seen.update(scope=scope, article_folder=article_folder)
        raise RuntimeError("stop here")

    monkeypatch.setattr("cockpit.ui.job_fetch_worker.locate", fake_locate)

    JobFetchWorker(
        "B123456", tmp_path,
        scope=IngestionScope.ARTICLE_SUBFOLDER,
        article_folder=tmp_path / "3rd",
    ).run()

    assert seen["scope"] is IngestionScope.ARTICLE_SUBFOLDER
    assert seen["article_folder"] == tmp_path / "3rd"


def test_ingestion_worker_forwards_the_plan():
    from cockpit.ingestion.service import IngestionPlan

    plan = IngestionPlan(article_revision="2nd")
    seen = {}

    class FakeService:
        def ingest(self, paths, progress=None, plan=None):
            seen["plan"] = plan
            raise RuntimeError("stop here")

    worker = IngestionWorker(FakeService(), [], plan=plan)
    worker.run()

    assert seen["plan"] is plan


def test_ingestion_worker_without_a_plan_passes_none():
    seen = {}

    class FakeService:
        def ingest(self, paths, progress=None, plan=None):
            seen["plan"] = plan
            raise RuntimeError("stop here")

    IngestionWorker(FakeService(), []).run()

    assert seen["plan"] is None
