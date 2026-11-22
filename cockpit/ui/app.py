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
from .crash_reporter import install_crash_reporter, LocalFileCrashSink, StderrCrashSink, GuiDialogCrashSink
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
    from cockpit.thread_retention import monotonic_ms

    started_at_ms = monotonic_ms()
    build_info = get_build_info()
    
    try:
        probe_outcome = resolve_app_data_root()
    except AppConfigError as e:
        _show_failure_dialog(e)
        sys.exit(2)
        
    from cockpit.ui.fault_log import rotate_faulthandler_log

    fault_path = probe_outcome.chosen_root / "faulthandler.log"
    fault_rotation_error = rotate_faulthandler_log(fault_path)
    fault_file = open(fault_path, "a", buffering=1, encoding="utf-8")
    # Kept for paths where main raises before finish_process; a clean exit closes the file itself.
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
    
    settings = QSettings(str(config.app_data_root / "settings.ini"), QSettings.Format.IniFormat)

    from cockpit.ui.ui_prefs import StyleController, RuntimeCalcSettingsController
    runtime_settings_controller = RuntimeCalcSettingsController(settings)

    bootstrapped = bootstrap(config, constants_provider=runtime_settings_controller.constants)
    if fault_rotation_error is not None:
        logging.getLogger(__name__).warning(
            "faulthandler.log couldn't be rotated (%s); it keeps growing", fault_rotation_error,
        )

    app = QApplication(sys.argv)

    # Patch 14 §6 (INV-T1): from here on the cyclic collector runs only on the GUI thread, before
    # MainWindow starts any worker.
    from cockpit.ui.collector import CollectorSettings, take_collection_onto_gui_thread

    collector = take_collection_onto_gui_thread(CollectorSettings.from_config(config))

    # Load new theme infrastructure
    try:
        theme = ThemeLoader.load(
            bundled_resource("ui/theme.json"),
            bundled_resource("ui/theme.schema.json")
        )
    except ConfigurationError as e:
        logging.getLogger(__name__).exception("Suppressed ConfigurationError in <main>")
        sys.stderr.write(f"ConfigurationError: {e}\n")
        # Exit or show error dialog (for now just print to stderr)
        sys.exit(2)

    # Phase 32 (2.4/2.5): stylesheet = schema theme + preset/font overrides
    style_controller = StyleController(app, theme, settings)
    app.setStyleSheet(style_controller.compose())

    # Phase 32 (3.2): unhandled exceptions also surface in a detailed dialog
    install_crash_reporter(
        crash_dir=config.file_storage_root.parent / "crash_reports",
        build_info=build_info,
        sinks=(
            LocalFileCrashSink(config.file_storage_root.parent / "crash_reports"),
            StderrCrashSink(),
            GuiDialogCrashSink()
        )
    )

    from cockpit.services.second_ops import SecondOpsSettingsController
    second_ops_settings_controller = SecondOpsSettingsController(settings)
    # Phase 49 (5.5): runs before any consumer reads terms(), and is an explicit
    # call rather than a side effect of the getter.
    term_merge_outcome = second_ops_settings_controller.migrate_shipped_terms()

    from cockpit.settings.source_root import SourceRootController
    source_root_controller = SourceRootController(settings)

    from cockpit.settings.mpn_library import MpnLibrarySettingsController
    mpn_library_controller = MpnLibrarySettingsController(settings)

    # Phase 59 §5.2: the Audit List's company short names.
    from cockpit.ui.company_names import CompanyNameController
    company_name_controller = CompanyNameController(settings)

    window = MainWindow(
        theme=theme,
        app=app,
        settings=settings,
        style_controller=style_controller,
        runtime_settings_controller=runtime_settings_controller,
        second_ops_settings_controller=second_ops_settings_controller,
        source_root_controller=source_root_controller,
        mpn_library_controller=mpn_library_controller,
        company_name_controller=company_name_controller,
        bootstrapped_app=bootstrapped,
        audit_read_svc=bootstrapped.audit_read_svc,
        checklist_svc=bootstrapped.checklist_svc,
        split_svc=bootstrapped.split_svc,
        completion_svc=bootstrapped.completion_svc,
        layout_query_svc=bootstrapped.layout_query_svc,
        pdf_renderer=bootstrapped.pdf_renderer,
        holiday_svc=bootstrapped.holiday_svc,
        collector=collector,
    )
    window.show()

    # A one-shot, not a visibility listener: showEvent would re-fire on every
    # minimise and restore and would need explicit disconnection.
    from PyQt6.QtCore import QTimer
    QTimer.singleShot(0, lambda: window.announce_term_merge(term_merge_outcome))
    QTimer.singleShot(0, lambda: window.announce_pattern_merge(window._pattern_merge_outcome))

    report = bootstrapped.reconciliation_report
    if report.errors or report.orphan_delete_failed:
        from cockpit.ui.widgets import ErrorDialog
        from cockpit.ui.error_messages import startup_cleanup_payload

        # Patch 14 §3: the payload used to carry the class Exception where the field is text, so building
        # this dialog raised TypeError and Cockpit couldn't start while the cleanup reported anything.
        dialog = ErrorDialog(startup_cleanup_payload(report), window)
        dialog.exec()

    # Patch 14 §10: one health line at startup, every interval, and at exit.
    from cockpit.layout.pdf_access import pdf_reading
    from cockpit.thread_retention import thread_retention
    from cockpit.ui.health import HealthLogger, HealthProviders, HealthReason

    health = HealthLogger(
        HealthProviders(
            started_at_ms=started_at_ms,
            clock=monotonic_ms,
            page=window.health_page,
            busy=window.health_busy,
            collector=collector,
            retention=thread_retention(),
            connection=bootstrapped.conn,
            pdf=pdf_reading,
        ),
        config.health_interval_ms,
    )

    def begin_session() -> None:
        collector.freeze_startup_heap()
        health.log_now(HealthReason.STARTUP)
        health.start()

    # Scheduled after the startup cleanup dialog has closed, so its nested loop can't run it early.
    QTimer.singleShot(0, begin_session)

    # Patch 14 §4 (INV-X1): the process ends without interpreter finalization, after explicit flushes.
    from cockpit.ui.process_exit import ExitFlushables, finish_process

    finish_process(app.exec(), window, ExitFlushables(settings, fault_file), health, thread_retention())


if __name__ == "__main__":
    main()
