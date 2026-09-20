"""Phase 48 sections 2, 6 and 7 -- the enrich path reaches the stored settings.

The exit condition for step 1 is behavioural: an operator who has stored
credentials reaches the pre-flight dialog instead of the "credentials not
configured" warning. Manual verification is what missed the original defect,
because the dialog stored a value, the store was real, and the read happened
somewhere else entirely.
"""

import pytest
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QMessageBox

from cockpit.persistence.repositories.bom_components import PersistedBomLine
from cockpit.services.mpn_library.module import MpnLibraryModule
from cockpit.settings.mpn_library import MpnLibrarySettingsController
from cockpit.ui.widgets.library_segment import LibrarySegment


@pytest.fixture
def library_module(tmp_path):
    module = MpnLibraryModule(tmp_path / "parts_library.db")
    yield module
    module.teardown()


@pytest.fixture
def controller(tmp_path):
    return MpnLibrarySettingsController(
        QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    )


@pytest.fixture
def bom_line():
    return PersistedBomLine(
        audit_id=1, source_file_id=1, part_number="ASSY-A", split_suffix="",
        work_order_ref="WO-1", find_number="1", component_mpn="RC0805FR-0710KL",
        description="RES 10K", mount_type="S"
    )


@pytest.fixture
def prompts(monkeypatch):
    """Records what the segment told the operator, and cancels the pre-flight."""
    recorded = {"warnings": [], "questions": [], "informations": []}
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda parent, title, text, *a, **k: recorded["warnings"].append((title, text))
    )
    monkeypatch.setattr(
        QMessageBox, "information",
        lambda parent, title, text, *a, **k: recorded["informations"].append((title, text))
    )

    def question(parent, title, text, *args, **kwargs):
        recorded["questions"].append((title, text))
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", question)
    return recorded


def _segment(qtbot, library_module, controller, bom_line):
    segment = LibrarySegment(library_module, controller)
    qtbot.addWidget(segment)
    segment.load(None, [bom_line])
    return segment


def test_stored_credentials_reach_the_preflight_dialog(
    qtbot, library_module, controller, bom_line, prompts
):
    controller.set_digikey_credentials("id-1", "secret-1")
    segment = _segment(qtbot, library_module, controller, bom_line)

    segment._on_enrich_clicked()

    assert prompts["warnings"] == []
    assert len(prompts["questions"]) == 1
    assert "Confirm Enrichment" == prompts["questions"][0][0]


def test_no_credentials_still_warns(qtbot, library_module, controller, bom_line, prompts):
    segment = _segment(qtbot, library_module, controller, bom_line)

    segment._on_enrich_clicked()

    assert prompts["questions"] == []
    assert prompts["warnings"][0][0] == "Credentials Required"


def test_half_stored_credentials_name_the_missing_field(
    qtbot, library_module, controller, bom_line, prompts
):
    controller._settings.setValue("library/digikey_client_id", "id-1")
    segment = _segment(qtbot, library_module, controller, bom_line)

    segment._on_enrich_clicked()

    assert prompts["questions"] == []
    assert "client secret" in prompts["warnings"][0][1]


def test_rejected_base_url_blocks_the_run(
    qtbot, library_module, controller, bom_line, prompts
):
    controller.set_digikey_credentials("id-1", "secret-1")
    controller.set_api_base_url("http://api.digikey.com")
    segment = _segment(qtbot, library_module, controller, bom_line)

    segment._on_enrich_clicked()

    assert prompts["questions"] == []
    assert prompts["warnings"][0][0] == "API Host Invalid"


def test_preflight_quotes_the_configured_ceiling_and_host(
    qtbot, library_module, controller, bom_line, prompts
):
    controller.set_digikey_credentials("id-1", "secret-1")
    controller.set_api_base_url("https://sandbox-api.digikey.com")
    controller.set_call_ceiling(37)
    segment = _segment(qtbot, library_module, controller, bom_line)

    segment._on_enrich_clicked()

    body = prompts["questions"][0][1]
    assert "Budget: 37 requests." in body
    assert "sandbox-api.digikey.com" in body
    assert "api.digikey.com/" not in body


def test_worker_receives_the_base_url_and_hands_it_to_the_gateway(
    tmp_path, monkeypatch
):
    """Phase 47 section 4.4: the worker takes values, not settings objects.
    Both the credentials and the host are read on the UI thread beforehand."""
    from cockpit.services.mpn_library import enrichment_worker
    from cockpit.services.mpn_library.enrichment_plan import EnrichmentPlan
    from cockpit.services.mpn_library.schema import migrate_library

    # The worker opens its own connection on its own thread, so it needs a
    # library file no other connection is bound to.
    worker_library_path = tmp_path / "worker_library.db"
    migrate_library(worker_library_path)

    constructed = {}

    class RecordingGateway:
        def __init__(self, api_base):
            constructed["api_base"] = api_base

        def initialize(self, credentials):
            constructed["credentials"] = credentials

    monkeypatch.setattr(enrichment_worker, "DigiKeyGateway", RecordingGateway)

    from cockpit.services.mpn_library.digikey_types import DigiKeyCredentials, RequestBudget
    from cockpit.persistence.clock import utcnow

    worker = enrichment_worker.EnrichmentWorker(
        plan=EnrichmentPlan(),
        credentials=DigiKeyCredentials("id-1", "secret-1"),
        budget=RequestBudget(remaining=10),
        library_path=worker_library_path,
        utcnow=utcnow,
        api_base="https://sandbox-api.digikey.com",
    )
    worker.run()

    assert constructed["api_base"] == "https://sandbox-api.digikey.com"
    assert constructed["credentials"].client_id == "id-1"


def test_bind_library_refuses_a_module_without_its_controller(qtbot, library_module):
    """A segment holding no controller would fail the same silent way the
    ambient settings object did, only later and at the enrich click."""
    from cockpit.ui.widgets.center_pager import CenterPager

    with pytest.raises(TypeError):
        CenterPager.bind_library(object(), library_module)
