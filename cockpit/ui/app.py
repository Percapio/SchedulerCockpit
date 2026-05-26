"""Application entry point."""

import pathlib
import sys
import traceback
import faulthandler
import atexit
import logging

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from .config import resolve_config, get_app_data_root
from .bootstrap import bootstrap
from .main_window import MainWindow
from .theme import ThemeLoader, Theme, ConfigurationError
from .crash_reporter import install_crash_reporter, LocalFileCrashSink, StderrCrashSink
from .data_migration import migrate_to_versioned_layout
from .runtime import bundled_resource
from cockpit._build_info import get_build_info


def main() -> None:
    build_info = get_build_info()
    pre_migration_root = get_app_data_root()
    
    fault_file = open(pre_migration_root / "faulthandler.log", "a", buffering=1, encoding="utf-8")
    atexit.register(fault_file.close)
    faulthandler.enable(file=fault_file, all_threads=True)
    
    install_crash_reporter(
        crash_dir=pre_migration_root,
        build_info=build_info,
        sinks=(LocalFileCrashSink(pre_migration_root), StderrCrashSink())
    )
    
    outcome = migrate_to_versioned_layout(pre_migration_root)
    config = resolve_config(root=outcome.target_root)
    
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
