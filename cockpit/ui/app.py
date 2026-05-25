"""Application entry point."""

import pathlib
import sys
import traceback

from PyQt6.QtWidgets import QApplication

from .config import resolve_config
from .bootstrap import bootstrap
from .main_window import MainWindow
from .theme import ThemeLoader, Theme, ConfigurationError


def main() -> None:
    try:
        config = resolve_config()
        bootstrapped = bootstrap(config)
    except Exception as e:
        sys.stderr.write(f"Bootstrap failed: {e}\n")
        traceback.print_exc(file=sys.stderr)
        
        # Determine error file path
        try:
            config_temp = resolve_config()
            err_path = config_temp.file_storage_root.parent / "bootstrap_error.txt"
        except Exception:
            err_path = pathlib.Path.cwd() / "bootstrap_error.txt"
            
        try:
            with open(err_path, "w", encoding="utf-8") as f:
                f.write(f"Bootstrap failed: {e}\n")
                traceback.print_exc(file=f)
        except Exception:
            pass
            
        sys.exit(2)

    app = QApplication(sys.argv)
    
    # Load new theme infrastructure
    try:
        ui_dir = pathlib.Path(__file__).parent
        theme = ThemeLoader.load(ui_dir / "theme.json", ui_dir / "theme.schema.json")
        app.setStyleSheet(theme.qss())
    except ConfigurationError as e:
        sys.stderr.write(f"ConfigurationError: {e}\n")
        # Exit or show error dialog (for now just print to stderr)
        sys.exit(2)

    window = MainWindow(
        theme=theme,
        app=app, 
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
