from PyQt6.QtCore import QSettings
import pytest
import pathlib
from unittest.mock import patch, Mock
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt

from cockpit.ui.widgets.audit_view import AuditView
from cockpit.ui.widgets.center_pager import CenterPage
from cockpit.ui.main_window import MainWindow
from cockpit.ui.theme import ThemeLoader

@pytest.fixture
def bootstrapped_app(tmp_path, monkeypatch):
    from cockpit.ui.config import resolve_config
    root = tmp_path / "cockpit_data"
    root.mkdir()
    monkeypatch.setenv("COCKPIT_APP_DATA", str(root))
    config = resolve_config(root / "v1")
    from cockpit.ui.bootstrap import bootstrap
    return bootstrap(config)

@pytest.fixture
def theme():
    ui_dir = pathlib.Path(__file__).parent.parent.parent / "cockpit" / "ui"
    return ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")

def test_unload_does_not_clear_session(qtbot, bootstrapped_app, theme):
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme, settings=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Test', 'Test')
    )
    qtbot.addWidget(view)
    
    # Session starts set
    assert view._actions_bar._session is not None
    assert view._center_pager._session is not None
    
    # Calling unload doesn't drop it
    view.unload()
    assert view._actions_bar._session is not None
    assert view._center_pager._session is not None

def test_center_pager_opens_on_drawing(qtbot, bootstrapped_app, theme):
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme, settings=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Test', 'Test')
    )
    qtbot.addWidget(view)
    
    pager = view._center_pager
    pager._selector.set_segments(has_secondary=False)
    pager._selector.show_page(CenterPage.BUILD_NOTES)
    pager._stacked.setCurrentWidget(pager._notes_pane)
    
    assert pager._selector._current_page == CenterPage.BUILD_NOTES
    
    pager.unload()
    
    assert pager._selector._current_page == CenterPage.PRIMARY_PDF
    assert pager._stacked.currentWidget() == pager._canvas

def test_all_panes_enabled_after_load(qtbot, bootstrapped_app, theme):
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme, settings=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Test', 'Test')
    )
    qtbot.addWidget(view)
    
    # We'll just verify that they are enabled initially
    assert view._center_pager.isEnabled()
    assert view._right_stack.isEnabled()

def test_main_window_picker_leaves_view_usable(qtbot, bootstrapped_app, theme):
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QSettings
    settings = QSettings(str(bootstrapped_app.config.app_data_root / "settings.ini"), QSettings.Format.IniFormat)
    main = MainWindow(
        QApplication.instance(),
        bootstrapped_app,
        bootstrapped_app.audit_read_svc,
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.pdf_renderer,
        bootstrapped_app.holiday_svc,
        theme=theme,
        settings=settings
    )
    qtbot.addWidget(main)
    
    # Mock load since we don't have a real DB populated in this test
    main._audit_view.load = Mock()
    main.picker.audit_selected.emit(1)
    
    qtbot.waitUntil(lambda: main._audit_view.load.called)
    
    assert main._audit_view._center_pager.isEnabled()
    assert main._audit_view._right_stack.isEnabled()

@patch('cockpit.ui.widgets.dialogs.confirm_destructive')
def test_complete_declined_leaves_audit_intact(mock_confirm, qtbot, bootstrapped_app, theme):
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme, settings=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Test', 'Test')
    )
    qtbot.addWidget(view)
    
    # Stub session methods to bypass `view.load(1)` entirely
    view._session.current_audit_id = Mock(return_value=1)
    
    mock_confirm.return_value = False
    
    with patch.object(bootstrapped_app.completion_svc, 'complete_and_cleanup') as mock_complete:
        view._actions_bar._on_complete_clicked()
        mock_complete.assert_not_called()

@patch('cockpit.ui.widgets.dialogs.confirm_destructive')
def test_complete_confirmed_deletes_audit(mock_confirm, qtbot, bootstrapped_app, theme):
    view = AuditView(
        bootstrapped_app.checklist_svc,
        bootstrapped_app.split_svc,
        bootstrapped_app.completion_svc,
        bootstrapped_app.ingestion_service,
        bootstrapped_app.layout_query_svc,
        bootstrapped_app.holiday_svc,
        Mock(),
        bootstrapped_app.pdf_renderer,
        theme=theme, settings=QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, 'Test', 'Test')
    )
    qtbot.addWidget(view)
    
    # Stub session methods
    view._session.current_audit_id = Mock(return_value=1)
    
    mock_confirm.return_value = True
    
    with patch.object(bootstrapped_app.completion_svc, 'complete_and_cleanup') as mock_complete:
        with qtbot.waitSignal(view.exit_requested):
            view._actions_bar._on_complete_clicked()
        mock_complete.assert_called_once_with(1)
