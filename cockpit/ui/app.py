"""Application entry point."""

import pathlib
import sys
import traceback
import faulthandler
import atexit
import logging

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.ui.config import resolve_config, resolve_app_data_root, AppConfigError, ProbeAttempt
from .bootstrap import bootstrap
from .main_window import MainWindow
from .theme import ThemeLoader, Theme, ConfigurationError
from .crash_reporter import install_crash_reporter, LocalFileCrashSink, StderrCrashSink
from .data_migration import migrate_to_versioned_layout
from .runtime import bundled_resource
from cockpit._build_info import get_build_info

def _show_failure_dialog(e: AppConfigError) -> None:
    from PyQt6.QtWidgets import QApplication, QMessageBox
    
    app = QApplication.instance() or QApplication(sys.argv)
    
    msg = QMessageBox()
    msg.setIcon(QMessageBox.Icon.Critical)
    msg.setWindowTitle("Cockpit cannot start")
    
    if e.error_reason == "all_probes_failed":
        text = "Cockpit could not find a writable folder to store its data.\n\nTried:\n"
        for attempt in e.probe_history:
            text += f"  • {attempt.candidate_path} — Permission denied\n"
        text += "\nWorkaround: set the COCKPIT_APP_DATA environment variable to a folder you can write to, then restart Cockpit. For example:\n  set COCKPIT_APP_DATA=C:\\Users\\you\\Documents\\Cockpit"
        msg.setText(text)
    elif e.error_reason == "multiple_claimed":
        text = "Cockpit found existing data in more than one location and cannot determine which is authoritative. Continuing would risk reading from one location and writing to the other.\n\nFound existing data at:\n"
        for attempt in e.probe_history:
            text += f"  • {attempt.candidate_path}\\v1\n"
        text += "\nWorkaround: either delete the data in the location you don't want to use, OR set the COCKPIT_APP_DATA environment variable to the location you do want to use. Restart Cockpit after either change."
        msg.setText(text)
    else:
        msg.setText(str(e))
        
    details = []
    for attempt in e.probe_history:
        details.append(f"Path: {attempt.candidate_path}")
        details.append(f"Label: {attempt.candidate_label}")
        details.append(f"Pre-existing DB: {attempt.pre_existing_db}")
        details.append(f"Error: {attempt.error}")
        details.append("")
    
    if details:
        msg.setDetailedText("\n".join(details))
        
    msg.exec()


def main() -> None:
    build_info = get_build_info()
    
    try:
        probe_outcome = resolve_app_data_root()
    except AppConfigError as e:
        _show_failure_dialog(e)
        sys.exit(2)
        
    fault_file = open(probe_outcome.chosen_root / "faulthandler.log", "a", buffering=1, encoding="utf-8")
    atexit.register(fault_file.close)
    faulthandler.enable(file=fault_file, all_threads=True)
    
    install_crash_reporter(
        crash_dir=probe_outcome.chosen_root,
        build_info=build_info,
        sinks=(LocalFileCrashSink(probe_outcome.chosen_root), StderrCrashSink())
    )
    
    outcome = migrate_to_versioned_layout(probe_outcome.chosen_root)
    config = resolve_config(
        root=outcome.target_root,
        probe_history=probe_outcome.probe_history
    )
    
    install_crash_reporter(
        crash_dir=config.file_storage_root.parent / "crash_reports",
        build_info=build_info,
        sinks=(LocalFileCrashSink(config.file_storage_root.parent / "crash_reports"), StderrCrashSink())
    )
    
    bootstrapped = bootstrap(config)

    app = QApplication(sys.argv)
    
    # Load new theme infrastructure
    try:
        theme = ThemeLoader.load(
            bundled_resource("ui/theme.json"),
            bundled_resource("ui/theme.schema.json")
        )
        app.setStyleSheet(theme.qss())
    except ConfigurationError as e:
        logging.getLogger(__name__).exception("Suppressed ConfigurationError in <main>")
        sys.stderr.write(f"ConfigurationError: {e}\n")
        # Exit or show error dialog (for now just print to stderr)
        sys.exit(2)

    settings = QSettings(str(config.app_data_root / "settings.ini"), QSettings.Format.IniFormat)

    window = MainWindow(
        theme=theme,
        app=app,
        settings=settings,
        bootstrapped_app=bootstrapped,
        audit_read_svc=bootstrapped.audit_read_svc,
        checklist_svc=bootstrapped.checklist_svc,
        split_svc=bootstrapped.split_svc,
        completion_svc=bootstrapped.completion_svc,
        audit_metadata_svc=bootstrapped.audit_metadata_svc,
        layout_query_svc=bootstrapped.layout_query_svc,
        pdf_renderer=bootstrapped.pdf_renderer
    )
    window.show()
    
    report = bootstrapped.reconciliation_report
    if report.errors or report.orphan_delete_failed:
        from cockpit.ui.widgets import ErrorDialog
        from cockpit.ui.error_messages import FailurePayload
        
        detail = []
        for audit_id, exc in report.errors:
            detail.append((f"Audit {audit_id}", str(exc)))
        for path, exc in report.orphan_delete_failed:
            detail.append((str(path), str(exc)))
            
        payload = FailurePayload(
            exception_class=Exception,
            title="Startup Cleanup Issues",
            summary="Some background cleanup tasks encountered errors. They will be retried on the next launch.",
            detail=detail,
            reason_code="RECONCILIATION_PARTIAL_FAILURE"
        )
        dialog = ErrorDialog(payload, window)
        dialog.exec()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
