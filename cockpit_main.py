"""Entry point shim for PyInstaller."""

import argparse
import sys
from cockpit.ui.app import main as real_main

def main() -> None:
    parser = argparse.ArgumentParser(description="Cockpit Manufacturing Audit Tool")
    parser.add_argument("--smoke-exit-after-bootstrap", action="store_true", help="Exit immediately after bootstrap completes (for CI smoke tests)")
    args, qt_args = parser.parse_known_args()
    
    if args.smoke_exit_after_bootstrap:
        import PyQt6.QtWidgets
        def fake_exec(*a, **kw):
            return 0
        PyQt6.QtWidgets.QApplication.exec = fake_exec
        
    sys.argv = [sys.argv[0]] + qt_args
    real_main()

if __name__ == "__main__":
    main()
