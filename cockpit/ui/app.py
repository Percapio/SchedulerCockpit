"""Application entry point."""

import pathlib
import sys
import traceback

from PyQt6.QtWidgets import QApplication

from .config import resolve_config
from .bootstrap import bootstrap
from .main_window import MainWindow


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
    
    # Load stylesheet
    try:
        qss_path = pathlib.Path(__file__).parent / "resources" / "styles.qss"
        if qss_path.exists():
            with open(qss_path, "r", encoding="utf-8") as f:
                app.setStyleSheet(f.read())
    except Exception as e:
        import logging
        logging.getLogger("cockpit.ui").warning(f"Could not load stylesheet: {e}")

    window = MainWindow(
        app, 
        bootstrapped,
        bootstrapped.audit_read_svc,
        bootstrapped.checklist_svc,
        bootstrapped.split_svc
    )
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
