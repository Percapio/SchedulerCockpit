import pytest
from unittest.mock import Mock
import pathlib

from cockpit.persistence.types import ActiveAudit, AuditStatus
from cockpit.services.views import AuditIdentityBanner, build_identity_banner, ActiveAuditView
from cockpit.ui.config import resolve_config
from cockpit.ingestion.service import IngestionService
from cockpit.services.checklist import ChecklistService
from cockpit.ui.widgets.audit_view import AuditView
from cockpit.ui.theme import ThemeLoader


def test_active_audit_coercion_idempotent():
    a = ActiveAudit(
        id=1, part_number="P1", schedule_job_id=None, work_order_ref="W1",
        split_suffix="", quantity=10, status="Not Clear", split_reason=None,
        traveler_metadata=None, created_at=None, updated_at=None, general_notes=None,
        ship_date=None, feeder_setuptime=None, smt_runtime=None, tht_runtime=None,
        aoi_runtime=None, ops_runtime=None, shipping_runtime=None, is_class_3=False,
        is_clean_process=False
    )
    assert a.status is AuditStatus.NOT_CLEAR
    assert isinstance(a.status, AuditStatus)

    b = ActiveAudit(
        id=1, part_number="P1", schedule_job_id=None, work_order_ref="W1",
        split_suffix="", quantity=10, status=AuditStatus.NOT_CLEAR, split_reason=None,
        traveler_metadata=None, created_at=None, updated_at=None, general_notes=None,
        ship_date=None, feeder_setuptime=None, smt_runtime=None, tht_runtime=None,
        aoi_runtime=None, ops_runtime=None, shipping_runtime=None, is_class_3=False,
        is_clean_process=False
    )
    assert b.status is AuditStatus.NOT_CLEAR
    assert isinstance(b.status, AuditStatus)

def test_active_audit_coercion_raises():
    with pytest.raises(ValueError):
        ActiveAudit(
            id=1, part_number="P1", schedule_job_id=None, work_order_ref="W1",
            split_suffix="", quantity=10, status="Invalid Status", split_reason=None,
            traveler_metadata=None, created_at=None, updated_at=None, general_notes=None,
            ship_date=None, feeder_setuptime=None, smt_runtime=None, tht_runtime=None,
            aoi_runtime=None, ops_runtime=None, shipping_runtime=None, is_class_3=False,
            is_clean_process=False
        )

def test_build_identity_banner_assembly_class_list_no_crash():
    view = ActiveAuditView(
        audit_id=1, part_number="P", work_order_ref="W", split_suffix="",
        quantity=1, split_reason=None, status=AuditStatus.NOT_CLEAR,
        tht_rows=[], notes_rows=[],
        traveler_metadata={"assembly_class": ["a list", "instead of scalar"]},
        has_pdf=False, tht_placement_count=0
    )
    banner = build_identity_banner(view)
    assert banner.assembly_class == ""


@pytest.fixture
def bootstrapped_app(tmp_path, monkeypatch):
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    config = resolve_config(root / "v1")
    from cockpit.ui.bootstrap import bootstrap
    return bootstrap(config)


def test_build_identity_banner_real_audit(bootstrapped_app):
    data_dir = pathlib.Path("backend/data")
    target_dir = data_dir / "B142006 Angel Aerial Systems"
    if not target_dir.exists():
        target_dir = data_dir / "B142000 Atlas Devices (ITAR)"
        
    if not target_dir.exists():
        pytest.skip("No sample trio found")

    files = [f for f in target_dir.glob("*") if f.name != "Thumbs.db"]
    ingest_svc = bootstrapped_app.ingestion_service
    
    active_audit = ingest_svc.ingest(files)
    audit_id = active_audit.id
            
    assert audit_id is not None
    
    chk_svc = bootstrapped_app.checklist_svc
    view = chk_svc.load_active_audit(audit_id)
    
    banner = build_identity_banner(view)
    assert banner.status == AuditStatus.NOT_CLEAR.value
    assert banner.sales_order != ""
    assert banner.customer != ""


def test_audit_view_integration_populates_panes(qtbot, bootstrapped_app):
    data_dir = pathlib.Path("backend/data")
    target_dir = data_dir / "B142006 Angel Aerial Systems"
    if not target_dir.exists():
        target_dir = data_dir / "B142000 Atlas Devices (ITAR)"
        
    if not target_dir.exists():
        pytest.skip("No sample trio found")

    files = [f for f in target_dir.glob("*") if f.name != "Thumbs.db"]
    ingest_svc = bootstrapped_app.ingestion_service
    
    active_audit = ingest_svc.ingest(files)
    audit_id = active_audit.id

    ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
    theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
    
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme
    )
    qtbot.addWidget(view)
    view.load(audit_id)
    
    # Assert panes are populated
    tht_pane = view.checklist_tht
    notes_pane = view._center_pager._notes_pane
    assert tht_pane.is_loaded()
    assert notes_pane.is_loaded()
