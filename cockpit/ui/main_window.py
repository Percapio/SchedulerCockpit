"""Main window."""

import pathlib
import sqlite3
from datetime import datetime, timedelta
from PyQt6.QtCore import Qt, QThread, QSettings, QTimer, QEvent, pyqtSignal
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QApplication, QDialog

from cockpit.ingestion.progress import ProgressStage
from cockpit.ui.bootstrap import BootstrappedApp
from cockpit.ui.error_messages import FailurePayload
from cockpit.ui.widgets import (
    DropArea, ProgressView, Toast, ErrorDialog,
    OpenAuditPicker, AuditView
)
from cockpit.ui.widgets.dialogs import confirm_destructive
from cockpit.ui.workers import IngestionWorker, AuditSummary, RenderWorker

from cockpit.services.audit_read import AuditReadService
from cockpit.persistence.types import AuditStatus
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.persistence.errors import PersistenceError
from cockpit.services.layout_query import LayoutQueryService
from cockpit.layout.renderer import PdfRenderer
from cockpit.ui.theme import Theme
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.ui.widgets.qt_lifecycle import FETCH_WATCHDOG_TIMEOUT_MS
from cockpit.ui.busy_state import (
    OPERATION_WORDING, PARKED_WORDING, BusyState, Entry, Operation, reconciler_can_write, unavailable_reason,
)
import logging
logger = logging.getLogger(__name__)

# FETCH_WATCHDOG_TIMEOUT_MS is imported above from qt_lifecycle: the source-root
# probe in settings_dialog scans the same shares on the same clock, and a widget
# cannot import the window that owns it. Same name, same value.

# Distinguishes "the operator declined" from "there is no plan", which is a
# legitimate value meaning today's unchanged ingest behaviour.
_PREFLIGHT_ABORTED = object()
RENDER_SHUTDOWN_TIMEOUT_MS = 10_000
# Phase 54 section 9: the list shows new scans at most this often during a
# reconciler pass, and once more when the pass ends.
REQUIREMENTS_LIST_REFRESH_MS = 1_000
# Phase 58 §6.2: X-ray mode, remembered on each PC across audits and restarts.
XRAY_MODE_SETTINGS_KEY = "audit_view/xray_mode"


class _IngestLatch:
    """Phase 55b §9: when the check after ingest starts, per ingest."""

    def __init__(self) -> None:
        self.audit_id: int | None = None
        self.handler_done = False
        self.worker_done = False


class MainWindow(QMainWindow):
    # Phase 55b §3.2: entries follow the busy state; Settings follows the last run.
    busy_changed = pyqtSignal(object)
    document_update_run_finished = pyqtSignal()

    @property
    def _operation_in_flight(self) -> bool:
        """Read-only, for the code and tests that read it: an operation is running (§3.1)."""
        return self._busy.operation is not None

    def __init__(
        self,
        app: QApplication,
        bootstrapped_app: BootstrappedApp,
        audit_read_svc: AuditReadService,
        checklist_svc: ChecklistService,
        split_svc: AuditSplitService,
        completion_svc: CompletionService,
        layout_query_svc: LayoutQueryService,
        pdf_renderer: PdfRenderer,
        holiday_svc,
        *,
        theme: Theme,
        settings: QSettings,
        style_controller=None,
        runtime_settings_controller=None,
        second_ops_settings_controller=None,
        source_root_controller=None,
        mpn_library_controller=None,
    ) -> None:
        super().__init__()
        self._theme = theme
        self._app = app
        self._bootstrapped = bootstrapped_app
        self._settings = settings
        self._style_controller = style_controller
        self._runtime_settings_controller = runtime_settings_controller
        self._second_ops_settings_controller = second_ops_settings_controller
        self._source_root_controller = source_root_controller
        self._mpn_library_controller = mpn_library_controller
        self._library_module = bootstrapped_app.library_module
        self._audit_read_svc = audit_read_svc
        self._holiday_svc = holiday_svc
        
        self.setWindowTitle("Local Audit & Routing Checklist Utility")
        self.resize(800, 600)
        
        self._busy = BusyState()
        # Phase 55b: the running update run, the per-ingest latch, the open Settings, and closing.
        self._update_run = None
        self._placements_latch: _IngestLatch | None = None
        self._settings_dialog = None
        self._closing = False
        # Set once a close has closed the connection; a later close does nothing.
        self._closed = False
        # Phase 55b §5.6: the update run's watchdog bound; tests shorten it.
        self._update_watchdog_ms = FETCH_WATCHDOG_TIMEOUT_MS
        self._close_requested = False
        self._worker = None
        self._thread = None
        self._orphaned_workers = []
        self._second_ops_review_dialogs = {}
        self._cleaning_report_dialogs = {}
        self._placements_report_dialogs = {}
        # Phase 57 section 9.2: the drawing worker, whichever call site starts it.
        self._drawing_thread = None
        self._drawing_worker = None
        self._drawing_dialog = None
        self._drawing_watchdog = QTimer(self)
        self._drawing_watchdog.setSingleShot(True)
        self._drawing_watchdog.setInterval(FETCH_WATCHDOG_TIMEOUT_MS)
        self._drawing_watchdog.timeout.connect(self._on_drawing_watchdog_timeout)
        self._next_registration_serial = 0
        
        self.stacked = QStackedWidget()
        self.setCentralWidget(self.stacked)
        
        self.drop_area = DropArea()
        self.drop_area.drop_received.connect(self._on_drop_received)
        self.drop_area.back_requested.connect(self._resolve_initial_page)
        self.stacked.addWidget(self.drop_area)
        
        stages = list(ProgressStage)
        self.progress_view = ProgressView(stages)
        self.progress_view.cancel_requested.connect(self._on_cancel_requested)
        self.stacked.addWidget(self.progress_view)
        
        self.picker = OpenAuditPicker(settings=settings)
        self.picker.audit_selected.connect(self._on_picker_audit_selected)
        self.picker.complete_requested.connect(self._on_complete_requested)
        self.picker.status_change_requested.connect(self._on_status_change_requested)
        self.picker.ship_date_change_requested.connect(self._on_ship_date_change_requested)
        self.picker.ops_per_board_change_requested.connect(self._on_ops_per_board_change_requested)
        self.picker.label_toggle_requested.connect(self._on_label_toggle_requested)
        self.picker.photos_toggle_requested.connect(self._on_photos_toggle_requested)
        self.picker.new_audit_requested.connect(self._on_picker_new_audit_requested)
        self.picker.fetch_job_requested.connect(self._on_picker_fetch_job_requested)
        self.picker.holidays_requested.connect(self._on_holidays_requested)
        self.picker.second_ops_requested.connect(self._on_overview_second_ops_requested)
        self.picker.xray_toggle_requested.connect(self._on_xray_toggle_requested)
        self.stacked.addWidget(self.picker)
        
        self._load_epoch = 0
        
        self._audit_view = AuditView(
            checklist_service=checklist_svc,
            split_service=split_svc,
            completion_service=completion_svc,
            ingestion_service=self._bootstrapped.ingestion_service,
            layout_query_service=layout_query_svc,
            release_service=self._bootstrapped.release_svc,
            setup_bom_service=self._bootstrapped.setup_bom_svc,
            pdf_renderer=pdf_renderer,
            theme=self._theme,
            audit_bom_component_repo=self._bootstrapped.audit_bom_component_repo,
            source_root_controller=source_root_controller,
        )
        self._audit_view.exit_requested.connect(self._on_dashboard_exit)
        self._audit_view.error_occurred.connect(self._on_failed)
        self._audit_view.ops_per_board_change_requested.connect(self._on_ops_per_board_change_requested)
        self._audit_view.second_ops_requested.connect(self._on_audit_second_ops_requested)
        self._audit_view.cleaning_report_requested.connect(self._on_audit_cleaning_report_requested)
        self._audit_view.drawing_fetch_requested.connect(self._on_drawing_fetch_requested)
        self._audit_view.drawing_drop_requested.connect(self._on_drawing_dropped)
        self._audit_view.placements_report_requested.connect(self._on_placements_report_requested)
        self._audit_view.document_update_check_requested.connect(self._on_check_family_requested)
        self._audit_view.set_entry_reason_provider(self._entry_reason)
        self._audit_view.xray_toggle_requested.connect(self._on_xray_toggle_requested)
        self.stacked.addWidget(self._audit_view)
        
        self.toast = Toast(self)
        self._term_merge_announced = False
        self._pattern_merge_announced = False
        from cockpit.services.requirements import load_vocabulary
        from cockpit.services.requirements_reconciler import RequirementsReconciler
        self._pattern_merge_outcome = load_vocabulary(self._settings)
        self._requirements_vocabulary = self._pattern_merge_outcome.vocabulary
        # An ingest holds its savepoint on a worker thread over the shared
        # connection; a UI-thread write meanwhile would join it.
        self._requirements_reconciler = RequirementsReconciler(
            self._bootstrapped.conn, self.current_requirements_vocabulary, self,
            can_write=lambda: reconciler_can_write(self._busy),
        )
        self._requirements_reconciler.audit_updated.connect(self._on_reconciler_audit_updated)
        self._requirements_reconciler.pass_completed.connect(self._on_reconciler_pass_completed)
        # A pass after the upgrade writes a row per open audit; repopulating
        # the list for each would rebuild it hundreds of times.
        self._requirements_list_refresh = QTimer(self)
        self._requirements_list_refresh.setSingleShot(True)
        self._requirements_list_refresh.setInterval(REQUIREMENTS_LIST_REFRESH_MS)
        self._requirements_list_refresh.timeout.connect(self._refresh_list_for_requirements)
        req_repo = self._bootstrapped.audit_repo.requirements_repo
        self.picker.model.set_evidence_loader(req_repo.find_evidence)
        self.picker.model.set_stale_scan_checker(self._is_audit_scan_stale)
        self._audit_view._identity_bar.set_evidence_loader(req_repo.find_evidence)
        self._audit_view._identity_bar.set_stale_scan_checker(self._is_audit_scan_stale)
        self._audit_view._actions_bar.set_vocabulary_provider(self.current_requirements_vocabulary)
        # Phase 58 §5, §6.2, §6.3: the open audit's scan first; X-ray mode remembered on this PC, and its
        # staleness from the identity bar's checker.
        self._audit_view.audit_loaded.connect(self._requirements_reconciler.promote)
        self._audit_view.set_xray_stale_checker(self._is_audit_scan_stale)
        self._audit_view.set_xray_mode_memory(self._load_xray_mode, self._store_xray_mode)
        self._pending_drawing_target = None
        self._pending_plan = None
        self._pending_article_folder = None
        from cockpit.services.ingestion_mode import IngestionMode
        self._pending_mode = IngestionMode.STANDARD

        self._style_controller = style_controller
        self._runtime_settings_controller = runtime_settings_controller
        self._second_ops_settings_controller = second_ops_settings_controller
        if runtime_settings_controller is not None:
            runtime_settings_controller.changed.connect(self._on_runtime_constants_changed)
        self._runtime_constants_dirty = False

        composer = style_controller.compose if style_controller is not None else None
        self._font_scale = FontScaleController(app, theme, settings, composer=composer)
        self._settings = settings

        # Phase 32 (2.4): Settings entry points from both views
        self.picker.settings_requested.connect(self._on_settings_requested)
        self._audit_view.settings_requested.connect(self._on_settings_requested)
        if style_controller is not None:
            style_controller.changed.connect(self._on_style_changed)
            # Apply the persisted font scale + preset overrides at startup.
            self._font_scale.reapply()
        
        self.picker.font_scale_change_requested.connect(self._font_scale.request_delta)
        
        self._render_thread = QThread()
        self._render_worker = RenderWorker(pdf_renderer)
        self._render_worker.moveToThread(self._render_thread)
        
        canvas = self._audit_view._layout_canvas
        canvas.request_render.connect(self._render_worker._on_request, Qt.ConnectionType.QueuedConnection)
        canvas.generation_bumped.connect(self._render_worker.note_latest_generation, Qt.ConnectionType.DirectConnection)
        self._render_worker.render_ready.connect(canvas.accept_render_result, Qt.ConnectionType.QueuedConnection)
        self._render_worker.render_error.connect(canvas.accept_render_failure, Qt.ConnectionType.QueuedConnection)
        
        self._audit_view.set_render_worker_alive(True)
        self._render_thread.start()
        
        self._resolve_initial_page()
        QTimer.singleShot(0, self._requirements_reconciler.trigger)
        
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(self._bootstrapped.config.idle_maintenance_interval_ms)
        self._idle_timer.timeout.connect(self._run_idle_maintenance)
        self._app.installEventFilter(self)
        self._idle_timer.start()
        
        self._midnight_timer = QTimer(self)
        self._midnight_timer.setSingleShot(True)
        self._midnight_timer.timeout.connect(self._on_midnight_timer)
        self._schedule_midnight_timer()
        
    def current_requirements_vocabulary(self):
        from cockpit.services.requirements import DEFAULT_VOCABULARY
        return getattr(self, "_requirements_vocabulary", DEFAULT_VOCABULARY)

    def announce_pattern_merge(self, outcome) -> None:
        """Surfaces the Process Requirements migration notice exactly once (§10.2)."""
        if getattr(self, "_pattern_merge_announced", False):
            return
        self._pattern_merge_announced = True
        if outcome is None or not outcome.added:
            return
        self.toast.show_toast(
            f"{len(outcome.added)} new process requirement patterns added",
            "Settings › Process Requirements",
        )

    def _is_audit_scan_stale(self, audit_id: int, stored_fingerprint: str) -> bool:
        try:
            cur = self._bootstrapped.conn.cursor()
            cur.execute("SELECT traveler_metadata FROM active_audits WHERE id = ?", (audit_id,))
            row = cur.fetchone()
            if not row:
                return False
            from cockpit.services.requirements_reconciler import _build_scan_inputs_for_audit
            from cockpit.services.requirements import scan_fingerprint
            inputs = _build_scan_inputs_for_audit(
                self._bootstrapped.conn,
                audit_id,
                row["traveler_metadata"],
                self.current_requirements_vocabulary(),
            )
            return scan_fingerprint(inputs) != stored_fingerprint
        except Exception:
            return False

    def _on_requirements_vocabulary_saved(self, vocab) -> None:
        self._requirements_vocabulary = vocab
        if hasattr(self, "_requirements_reconciler"):
            self._requirements_reconciler.trigger(notify_toast=True)

    def _load_xray_mode(self) -> bool:
        return bool(self._settings.value(XRAY_MODE_SETTINGS_KEY, False, type=bool))

    def _store_xray_mode(self, on: bool) -> None:
        self._settings.setValue(XRAY_MODE_SETTINGS_KEY, bool(on))

    def _on_reconciler_audit_updated(self, audit_id: int) -> None:
        if self._audit_view.current_audit_id() == audit_id:
            self._audit_view.refresh_audit_fields(audit_id)
            # Phase 58 §6.8: the canvas re-reads the scan's X-ray locations.
            self._audit_view.reload_xray_state()
        if not self._requirements_list_refresh.isActive():
            self._requirements_list_refresh.start()

    def _refresh_list_for_requirements(self) -> None:
        """Repaints the list with new scans, at most once per interval, keeping your place in it."""
        if self.stacked.currentWidget() == self.picker:
            self.picker.populate(self._audit_read_svc.list_open(), keep_scroll=True)

    def _on_reconciler_pass_completed(self, summary, notify_toast: bool) -> None:
        if summary.written > 0:
            self._requirements_list_refresh.stop()
            self._refresh_list_for_requirements()
        if notify_toast:
            self.toast.show_toast(
                f"Process requirements: {summary.changed_state} audits changed state",
                "Settings › Process Requirements",
            )

    def _on_xray_toggle_requested(self, audit_id: int, checked: bool) -> None:
        from cockpit.persistence.repositories.requirements import set_xray_images_done
        try:
            res = set_xray_images_done(self._bootstrapped.conn, audit_id, checked)
            if res.is_err:
                self.toast.show_toast(
                    "X-ray & save images is not required",
                    "The X-ray requirement changed for this audit",
                )
        except (PersistenceError, sqlite3.Error) as e:
            self._on_failed(FailurePayload.from_exception(e, "X-ray update failed"))
            return
        if self._audit_view.current_audit_id() == audit_id:
            self._audit_view.refresh_audit_fields(audit_id)
        if self.stacked.currentWidget() == self.picker:
            self.picker.populate(self._audit_read_svc.list_open())

    def _on_runtime_constants_changed(self) -> None:
        self._runtime_constants_dirty = True

    def announce_term_merge(self, outcome) -> None:
        """Surfaces the 2nd OPS migration notice exactly once.

        pre:  called after the window is shown
        post: a toast is raised iff outcome.added is non-empty; a second call
              raises nothing
        """
        if self._term_merge_announced:
            return
        self._term_merge_announced = True
        if outcome is None or not outcome.added:
            return
        self.toast.show_toast(
            f"{len(outcome.added)} new 2nd OPS terms added",
            "Settings › 2nd OPS"
        )

    def _recompute_and_refresh(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            summary = self._bootstrapped.runtime_calc_svc.recompute_all()
            self.picker.populate(self._audit_read_svc.list_open())
            if summary.failed > 0:
                self.toast.show_toast(
                    "Recompute incomplete",
                    f"{summary.failed} of {summary.attempted} audits could not be recomputed"
                )
        finally:
            QApplication.restoreOverrideCursor()

    def _on_settings_requested(self) -> None:
        if self._style_controller is None:
            return
        self._runtime_constants_dirty = False
        try:
            from cockpit.ui.widgets.settings_dialog import SettingsDialog
            dialog = SettingsDialog(
                self._style_controller,
                self._font_scale,
                self._runtime_settings_controller,
                self._second_ops_settings_controller,
                self._bootstrapped.config,
                self,
                source_root_controller=self._source_root_controller,
                mpn_library_controller=self._mpn_library_controller,
                enrichment_in_flight=self._audit_view.is_enrichment_in_flight,
                settings=self._settings,
                conn=self._bootstrapped.conn,
                current_audit_id=self._audit_view.current_audit_id(),
                initial_vocabulary=self.current_requirements_vocabulary(),
                document_updates=self._document_update_hooks(),
            )
            dialog.reset_requested.connect(self._on_reset_requested)
            dialog.requirements_vocabulary_saved.connect(self._on_requirements_vocabulary_saved)
            # Phase 55b §3.2: bound methods, which Qt drops with the dialog; disconnected below too.
            dialog.document_update_check_requested.connect(self._on_check_all_requested)
            self.busy_changed.connect(dialog.refresh_document_updates)
            self.document_update_run_finished.connect(dialog.refresh_document_updates)
            self._settings_dialog = dialog
            try:
                dialog.exec()
            finally:
                self._settings_dialog = None
                for signal in (self.busy_changed, self.document_update_run_finished):
                    try:
                        signal.disconnect(dialog.refresh_document_updates)
                    except (TypeError, RuntimeError):
                        pass
        finally:
            if (not self._closing and self._runtime_constants_dirty
                    and self._runtime_settings_controller is not None):
                self._recompute_and_refresh()

    def _on_reset_requested(self) -> None:
        reason = self._entry_reason(Entry.RESET)
        if reason is not None:
            QMessageBox.warning(self, "Cannot Reset", f"{reason} Please wait for it to finish.")
            return

        for audit_id, (serial, dialog) in self._second_ops_review_dialogs.items():
            if dialog.is_read_in_flight():
                QMessageBox.warning(self, "Cannot Reset", f"A 2nd OPS review window is currently reading audit {audit_id}'s workbook.")
                return

        expected_audit_count = len(self._audit_read_svc.list_open())

        from cockpit.ui.widgets.dialogs import TypedConfirmationDialog
        confirm = TypedConfirmationDialog("Reset Application Data", expected_audit_count, self)
        if not confirm.exec():
            return

        for serial, dialog in list(self._second_ops_review_dialogs.values()):
            dialog.close()

        self._audit_view.unload()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from cockpit.services.reset_service import reset_application_data, ResetAborted, ResetAbortCause
            outcome_or_aborted = reset_application_data(
                self._bootstrapped.audit_repo,
                self._bootstrapped.source_file_repo,
                self._bootstrapped.storage_reaper,
                self._bootstrapped.conn,
                expected_audit_count
            )

            if isinstance(outcome_or_aborted, ResetAborted):
                QApplication.restoreOverrideCursor()
                aborted = outcome_or_aborted
                if aborted.cause == ResetAbortCause.CAPTURE_FAILED:
                    msg = f"The reset did not run; no audit or file was changed.\n\n{aborted.underlying}"
                elif aborted.cause == ResetAbortCause.DELETE_FAILED:
                    msg = f"The reset was rolled back; no audit or file was changed.\n\n{aborted.underlying}"
                elif aborted.cause == ResetAbortCause.COUNT_MISMATCH:
                    msg = f"The audit count changed from {expected_audit_count} to {aborted.observed_count}. Nothing was changed."
                else:
                    msg = "Reset aborted."
                QMessageBox.critical(self, "Reset Aborted", msg)
                return

            outcome = outcome_or_aborted
            sender = self.sender()
            if hasattr(sender, "accept"):
                sender.accept()

            self._show_drop_area()
            self.drop_area.set_back_visible(False)

            if outcome.unreaped:
                self.toast.show_toast(
                    "Reset incomplete",
                    f"{len(outcome.unreaped)} files could not be deleted. They will be cleared on the next launch."
                )
        finally:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()

    def _on_style_changed(self) -> None:
        # Preset/font change: regenerate stylesheet at the current scale and
        # repaint the list so semantic column colors pick up the new palette.
        self._font_scale.reapply()
        self.picker.table_view.viewport().update()
        self._audit_view.invalidate_notes_render()

    def _run_idle_maintenance(self) -> None:
        from cockpit.persistence.connection import run_idle_maintenance
        run_idle_maintenance(self._bootstrapped.conn)
        if reconciler_can_write(self._busy) and hasattr(self, "_requirements_reconciler"):
            self._requirements_reconciler.trigger()

    def _schedule_midnight_timer(self) -> None:
        now = datetime.now()
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        ms = max(1000, int((tomorrow - now).total_seconds() * 1000))
        self._midnight_timer.start(ms)

    def _on_midnight_timer(self) -> None:
        self._reload_list()
        self._schedule_midnight_timer()

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (
            QEvent.Type.KeyPress, QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonPress, QEvent.Type.Wheel,
            QEvent.Type.TouchBegin, QEvent.Type.TouchUpdate
        ):
            if hasattr(self, "_idle_timer"):
                self._idle_timer.start()
        return super().eventFilter(obj, event)
        
    def _show_drop_area(self) -> None:
        # Second exit-to-list path alongside _reload_list; both release before
        # switching. setCurrentWidget only hides.
        self._audit_view.unload()
        open_audits = self._audit_read_svc.list_open()
        self.drop_area.set_back_visible(len(open_audits) > 0)
        self.stacked.setCurrentWidget(self.drop_area)

    def _resolve_initial_page(self) -> None:
        open_audits = self._audit_read_svc.list_open()
        if not open_audits:
            self._show_drop_area()
        elif len(open_audits) == 1:
            self._audit_view.load(open_audits[0].audit_id)
            self.stacked.setCurrentWidget(self._audit_view)
        else:
            self.picker.populate(open_audits)
            self.stacked.setCurrentWidget(self.picker)
            
    def _on_picker_audit_selected(self, audit_id: int) -> None:
        self._load_epoch += 1
        epoch = self._load_epoch
        self.stacked.setCurrentWidget(self._audit_view)
        
        def do_load():
            if epoch != self._load_epoch:
                return
            self._sync_mpn_library_state()
            self._audit_view.load(audit_id)
            
        QTimer.singleShot(0, do_load)
            
    def _sync_mpn_library_state(self) -> None:
        """Construct or release the optional MPN library module for this session.

        The module is held here, not on BootstrappedApp: that is a frozen
        dataclass, so assigning to its library_module field raised
        FrozenInstanceError on the first audit opened with the feature on.
        """
        if not self._mpn_library_controller:
            return

        enabled = self._mpn_library_controller.is_enabled()

        if enabled and self._library_module is None:
            from cockpit.services.mpn_library.module import MpnLibraryModule
            db_path = self._bootstrapped.config.app_data_root / "parts_library.db"
            try:
                self._library_module = MpnLibraryModule(db_path)
            except Exception:
                # Per Phase 47a section 4.3 an unusable library disables the
                # feature for the session and leaves the rest of the
                # application untouched. It must never fail an audit load.
                logger.exception("MPN library unavailable at %s; feature disabled", db_path)
                self._library_module = None
            self._audit_view.bind_library(self._library_module, self._mpn_library_controller)

        elif not enabled and self._library_module is not None:
            try:
                self._library_module.teardown()
            except Exception:
                logger.exception("MPN library teardown failed")
            self._library_module = None
            self._audit_view.bind_library(None, None)

    def _complete_confirm_body(self, audit_id: int) -> str:
        body = "This permanently deletes the audit and its source files. This cannot be undone."
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            from cockpit.services.requirements_reconciler import pending_xray_warning
            extra_warning = pending_xray_warning(
                self._bootstrapped.conn,
                audit_id,
                self.current_requirements_vocabulary(),
            )
        except Exception:
            logger.exception("Could not check pending X-ray warning for audit %s", audit_id)
            extra_warning = None
        finally:
            QApplication.restoreOverrideCursor()
        if self.stacked.currentWidget() == self.picker:
            self.picker.populate(self._audit_read_svc.list_open())
        if extra_warning:
            body = f"{body}\n\n{extra_warning}"
        return body

    def _on_complete_requested(self, audit_id: int) -> None:
        entry = self._second_ops_review_dialogs.get(audit_id)
        if entry is not None and entry[1].is_read_in_flight():
            QMessageBox.warning(self, "Cannot Complete", "A 2nd OPS review window is currently reading this audit's workbook.")
            return

        if not confirm_destructive(
            title="Complete audit",
            body=self._complete_confirm_body(audit_id),
            confirm_label="Complete",
            parent=self
        ):
            return
            
        if entry is not None:
            entry[1].close()

        try:
            self._bootstrapped.completion_svc.complete_and_cleanup(audit_id)
        except (CleanupFailedError, PersistenceError) as e:
            self._on_failed(FailurePayload.from_exception(e, "Completion failed"))
        finally:
            self._invalidate_audit_view_if_loaded(audit_id)
            self._reload_list()
            
    def _on_status_change_requested(self, audit_id: int, target: AuditStatus) -> None:
        try:
            self._bootstrapped.audit_repo.transition_status(audit_id, target)
        except PersistenceError as e:
            self._on_failed(FailurePayload.from_exception(e, "Status transition failed"))
            return
        self._reload_list()
        
    def _on_ship_date_change_requested(self, audit_id: int, ship_date) -> None:
        try:
            self._bootstrapped.audit_repo.set_ship_date(audit_id, ship_date)
        except PersistenceError as e:
            self._on_failed(FailurePayload.from_exception(e, "Ship date update failed"))
            return
        self._reload_list()

    def _on_ops_per_board_change_requested(self, audit_id: int, value: float | None) -> None:
        try:
            self._bootstrapped.audit_repo.set_ops_per_board_min(audit_id, value)
            self._bootstrapped.runtime_calc_svc.persist(audit_id)
        except PersistenceError as e:
            self._on_failed(FailurePayload.from_exception(e, "OPS update failed"))
            return
        self._invalidate_audit_view_if_loaded(audit_id)
        self._reload_list()

    def _on_label_toggle_requested(self, audit_id: int, value: bool) -> None:
        try:
            self._bootstrapped.audit_repo.set_is_labeled(audit_id, value)
        except PersistenceError as e:
            self._on_failed(FailurePayload.from_exception(e, "Label update failed"))
            return
        self._reload_list()

    def _on_photos_toggle_requested(self, audit_id: int, value: bool) -> None:
        try:
            self._bootstrapped.audit_repo.set_are_photos_uploaded(audit_id, value)
        except PersistenceError as e:
            self._on_failed(FailurePayload.from_exception(e, "Photos update failed"))
            return
        self._reload_list()

    def _invalidate_audit_view_if_loaded(self, audit_id: int) -> None:
        self._audit_view.discard_if_showing(audit_id)
        
    def _on_picker_new_audit_requested(self, mode=None) -> None:
        from cockpit.services.ingestion_mode import IngestionMode

        self._pending_mode = mode or IngestionMode.STANDARD
        self._show_drop_area()

    def _on_picker_fetch_job_requested(self, mode=None) -> None:
        from cockpit.services.ingestion_mode import IngestionMode

        self._pending_mode = mode or IngestionMode.STANDARD
        self._pending_article_folder = None
        if self._source_root_controller is None:
            return
            
        from cockpit.settings.source_root import SourceRootState
        state, root_path = self._source_root_controller.source_root()
        if state != SourceRootState.CONFIGURED:
            QMessageBox.information(self, "Setup Required", "Please configure the source share root in Settings first.")
            return
            
        last_job = self._settings.value("ingestion/last_job_number", "")
        
        from cockpit.ui.widgets.fetch_dialog import FetchJobDialog
        self._fetch_dialog = FetchJobDialog(str(last_job), self)
        self._fetch_dialog.fetch_requested.connect(lambda jn: self._start_job_fetch(jn, root_path))
        self._fetch_dialog.rejected.connect(self._on_fetch_dialog_cancelled)
        
        # Don't use exec() here because we want to run the fetch concurrently and let the dialog close or update
        # Wait, if it's a modal dialog and we show() it, we can't block. Wait, QDialog.exec() runs a nested event loop.
        # If we use exec(), the signals still fire! So exec() is fine, we just close it from the signal handlers.
        self._fetch_dialog.exec()

    def _start_job_fetch(self, job_number: str, root_path: pathlib.Path) -> None:
        if self._operation_in_flight:
            return
            
        self._begin(Operation.JOB_FETCH)
        
        from cockpit.ui.job_fetch_worker import JobFetchWorker
        self._fetch_thread = QThread()
        self._fetch_worker = JobFetchWorker(
            job_number, root_path,
            scope=self._pending_mode.scope,
            article_folder=self._pending_article_folder,
        )
        self._fetch_worker.moveToThread(self._fetch_thread)
        
        self._fetch_thread.started.connect(self._fetch_worker.run)
        
        self._fetch_worker.succeeded_signal.connect(self._on_fetch_succeeded)
        self._fetch_worker.failed_signal.connect(self._on_fetch_failed)
        
        self._fetch_watchdog = QTimer(self)
        self._fetch_watchdog.setSingleShot(True)
        self._fetch_watchdog.timeout.connect(self._on_fetch_watchdog_timeout)
        self._fetch_watchdog.start(FETCH_WATCHDOG_TIMEOUT_MS)
        
        self._fetch_thread.start()

    def _on_fetch_dialog_cancelled(self) -> None:
        if self._operation_in_flight and hasattr(self, "_fetch_thread") and self._fetch_thread is not None:
            self._abandon_fetch_worker()

    def _abandon_fetch_worker(self) -> None:
        # Phase 46 abandonment, corrected by Patch 11 section 6.
        self._end_operation()
        if hasattr(self, "_fetch_watchdog"):
            self._fetch_watchdog.stop()
            
        if hasattr(self, "_fetch_thread") and self._fetch_thread is not None:
            thread, worker = self._fetch_thread, self._fetch_worker
            entry = (thread, worker)

            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            # Retention without a discard is a leak. _IN_FLIGHT_PROBES gets this
            # right and this list did not: entries were appended and never
            # removed.
            thread.finished.connect(lambda e=entry: self._discard_orphan(e))
            self._orphaned_workers.append(entry)

            # Disconnect signals so they don't do anything if they ever wake up
            try:
                worker.succeeded_signal.disconnect(self._on_fetch_succeeded)
                worker.failed_signal.disconnect(self._on_fetch_failed)
            except Exception:
                pass

            # quit() is not wait(). wait() blocks the UI thread for exactly as
            # long as the share does, which is the freeze abandonment exists to
            # avoid; quit() posts a request and returns. Without it nothing ends
            # this thread at all: the handlers disconnected above were the only
            # callers of quit(), and QThread.run() is an event loop that runs
            # until asked to stop. A worker still inside its blocking call has
            # not entered exec() yet, so this latches and takes effect the
            # moment it is reached.
            try:
                thread.quit()
            except RuntimeError:
                pass

            self._fetch_thread = None
            self._fetch_worker = None

    def _discard_orphan(self, entry) -> None:
        try:
            self._orphaned_workers.remove(entry)
        except ValueError:
            pass

    # --- Phase 50 section 4.4: drawing fetch -----------------------------

    def _on_drawing_fetch_requested(self, audit_id: int, secondary: bool) -> None:
        """Locates a drawing on the share and attaches it to the audit.

        Reuses the Phase 46 worker and watchdog rather than a second copy: the
        blocking-stat hazard and its abandonment contract are identical.
        """
        if self._operation_in_flight:
            return

        from cockpit.settings.source_root import SourceRootState

        if self._source_root_controller is None:
            return
        state, root_path = self._source_root_controller.source_root()
        if state != SourceRootState.CONFIGURED:
            QMessageBox.information(
                self, "Setup Required",
                "Please configure the source share root in Settings first."
            )
            return

        audit = self._bootstrapped.audit_repo.find_by_id(audit_id)
        if audit is None:
            return

        self._pending_drawing_target = (audit_id, secondary)
        self._begin(Operation.DRAWING_FETCH)

        from cockpit.ui.job_fetch_worker import DrawingFetchWorker
        self._fetch_thread = QThread()
        self._fetch_worker = DrawingFetchWorker(audit.part_number, root_path)
        self._fetch_worker.moveToThread(self._fetch_thread)

        self._fetch_thread.started.connect(self._fetch_worker.run)
        self._fetch_worker.succeeded_signal.connect(self._on_drawing_fetch_succeeded)
        self._fetch_worker.failed_signal.connect(self._on_fetch_failed)

        self._fetch_watchdog = QTimer(self)
        self._fetch_watchdog.setSingleShot(True)
        self._fetch_watchdog.timeout.connect(self._on_fetch_watchdog_timeout)
        self._fetch_watchdog.start(FETCH_WATCHDOG_TIMEOUT_MS)

        self._fetch_thread.start()

    # --- Phase 57 section 9.2: Update > Drawing on the drawing worker --------

    def _on_drawing_dropped(self, dialog, pdf_path: pathlib.Path) -> None:
        self._start_drawing_update(dialog.audit_id, pdf_path, dialog)

    def _report_drawing_problem(self, dialog, text: str) -> None:
        if dialog is not None and dialog.isVisible():
            dialog.show_error(text)
        else:
            QMessageBox.warning(self, "Drawing not updated", text)

    def _start_drawing_update(self, audit_id: int, pdf_path: pathlib.Path, dialog=None) -> None:
        """Copies, parses and fits a primary drawing on a worker MainWindow owns (Phase 57 section 9.2).

        pre:  UI thread; dialog, when given, is the busy AddDrawingDialog the drop came from
        post: the worker runs under the share-call watchdog, with _operation_in_flight set so Phase
              55's exclusion holds; its result is applied here, on the UI thread, and reported to the
              dialog or in a message
        """
        if self._operation_in_flight or self._drawing_worker is not None:
            self._report_drawing_problem(dialog, "Another operation is running; try again when it finishes.")
            return
        from uuid import uuid4
        from cockpit.services.drawing_update import read_drawing_update_inputs
        from cockpit.ui.workers.drawing_update_worker import DrawingUpdateWorker

        try:
            inputs = read_drawing_update_inputs(audit_id, self._bootstrapped.update_repositories)
        except PersistenceError as exc:
            logger.exception("Reading the audit for a drawing update failed")
            self._report_drawing_problem(dialog, f"Database error: {exc}")
            return
        if inputs is None:
            self._report_drawing_problem(dialog, "The audit no longer exists.")
            return

        registry = self._bootstrapped.parser_registry
        thread = QThread()
        worker = DrawingUpdateWorker(
            inputs, pathlib.Path(pdf_path), self._bootstrapped.config.file_storage_root,
            registry.pdf_layout_parser, registry.bom_parser, uuid4().hex,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.prepared.connect(self._on_drawing_update_prepared)
        worker.failed.connect(self._on_drawing_update_failed)
        worker.beacon.started.connect(self._drawing_watchdog.start)
        worker.beacon.finished.connect(self._drawing_watchdog.stop)
        if dialog is not None:
            dialog.cancel_requested.connect(self._on_drawing_update_cancel)

        self._drawing_thread = thread
        self._drawing_worker = worker
        self._drawing_dialog = dialog
        self._begin(Operation.DRAWING_UPDATE)
        thread.start()

    def _finish_drawing_worker(self) -> None:
        self._drawing_watchdog.stop()
        thread, worker = self._drawing_thread, self._drawing_worker
        self._drawing_thread = None
        self._drawing_worker = None
        self._drawing_dialog = None
        self._end_operation()
        if thread is not None:
            thread.quit()
            thread.wait()
            thread.deleteLater()
        if worker is not None:
            worker.deleteLater()

    def _on_drawing_update_prepared(self, plan) -> None:
        from cockpit.services.drawing_update import (
            DrawingUpdated, apply_drawing_update, failure_text, success_text,
        )
        from cockpit.services.document_updates import discard_copies

        dialog = self._drawing_dialog
        cancelled = self._drawing_worker is not None and self._drawing_worker.stop.is_set()
        self._finish_drawing_worker()
        repositories = self._bootstrapped.update_repositories
        if cancelled:
            if plan.staged_pdf.copied_by_this_run:
                discard_copies((plan.staged_pdf,), repositories.source_files)
            return

        outcome = apply_drawing_update(plan, repositories, self._bootstrapped.runtime_calc_svc)
        if isinstance(outcome, DrawingUpdated):
            if dialog is not None and dialog.isVisible():
                dialog.accept()
            else:
                self._audit_view.load(outcome.audit_id)
            self.toast.show_toast(success_text(outcome), "")
            return
        self._report_drawing_problem(dialog, failure_text(outcome))

    def _on_drawing_update_failed(self, failed) -> None:
        from cockpit.services.document_updates import Cancelled
        from cockpit.services.drawing_update import discard_drawing_copy, failure_text

        dialog = self._drawing_dialog
        cancelled = self._drawing_worker is not None and self._drawing_worker.stop.is_set()
        self._finish_drawing_worker()
        discard_drawing_copy(failed, self._bootstrapped.update_repositories)
        if cancelled or isinstance(failed.failure, Cancelled):
            return
        self._report_drawing_problem(dialog, failure_text(failed.failure))

    def _on_drawing_update_cancel(self) -> None:
        """Cancel while the worker runs: the stop flag is set and the dialog closes itself."""
        if self._drawing_worker is not None:
            self._drawing_worker.stop.set()
        self._drawing_dialog = None

    def _abandon_drawing_worker(self) -> None:
        """Phase 55 section 7's abandonment: stop, disconnect, orphan, and never wait on the share."""
        thread, worker = self._drawing_thread, self._drawing_worker
        self._drawing_watchdog.stop()
        self._drawing_thread = None
        self._drawing_worker = None
        self._drawing_dialog = None
        self._end_operation()
        if thread is None or worker is None:
            return
        worker.stop.set()
        for signal, slot in (
            (worker.prepared, self._on_drawing_update_prepared),
            (worker.failed, self._on_drawing_update_failed),
            (worker.beacon.started, self._drawing_watchdog.start),
            (worker.beacon.finished, self._drawing_watchdog.stop),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        entry = (thread, worker)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda e=entry: self._discard_orphan(e))
        self._orphaned_workers.append(entry)
        try:
            thread.quit()
        except RuntimeError:
            pass

    def _on_drawing_watchdog_timeout(self) -> None:
        dialog = self._drawing_dialog
        self._abandon_drawing_worker()
        self._report_drawing_problem(
            dialog,
            f"No answer from the share within {FETCH_WATCHDOG_TIMEOUT_MS // 1000} s; the drawing wasn't added.",
        )

    def _hand_orphans_to_cpp(self) -> None:
        """Phase 55 section 7: Python's teardown must not destroy a thread still blocked on the share."""
        from PyQt6 import sip
        for thread, worker in list(self._orphaned_workers):
            for obj in (worker, thread):
                try:
                    sip.transferto(obj, None)
                except Exception:
                    logger.exception("An orphaned worker couldn't be handed to C++ ownership")

    # --- Phase 57 section 10.5: Placements report --------------------------

    def _unregister_placements_report_dialog(self, audit_id: int, serial: int) -> None:
        entry = self._placements_report_dialogs.get(audit_id)
        if entry is not None and entry[0] == serial:
            self._placements_report_dialogs.pop(audit_id)

    def _on_placements_report_requested(self, audit_id: int) -> None:
        entry = self._placements_report_dialogs.get(audit_id)
        if entry is not None:
            dialog = entry[1]
            dialog.raise_()
            dialog.activateWindow()
            return
        from cockpit.ui.widgets.placements_report_dialog import PlacementsReportDialog
        try:
            report = self._bootstrapped.placement_query_svc.report(audit_id)
        except PersistenceError as exc:
            logger.exception("Reading the placements report failed")
            self._on_failed(FailurePayload.from_exception(exc, "Could not read the placements report"))
            return
        dialog = PlacementsReportDialog(audit_id, report, self)
        serial = self._next_registration_serial
        self._next_registration_serial += 1
        self._placements_report_dialogs[audit_id] = (serial, dialog)
        dialog.finished.connect(lambda result_code, a=audit_id, s=serial: self._unregister_placements_report_dialog(a, s))
        dialog.destroyed.connect(lambda obj=None, a=audit_id, s=serial: self._unregister_placements_report_dialog(a, s))
        dialog.show()

    # --- Phase 55b section 3: the busy state -------------------------------

    def _set_busy(self, busy: BusyState) -> None:
        self._busy = busy
        reason = self._entry_reason(Entry.NEW_AUDIT)
        try:
            self.picker.set_new_audit_unavailable(reason)
            self.drop_area.setToolTip(reason or "")
        except (AttributeError, RuntimeError):
            pass
        self.busy_changed.emit(busy)

    def _begin(self, operation: Operation) -> None:
        self._set_busy(self._busy.begin(operation))

    def _end_operation(self) -> None:
        self._set_busy(self._busy.end())

    def _source_root_state_name(self) -> str | None:
        if self._source_root_controller is None:
            return None
        state, _ = self._source_root_controller.source_root()
        return state.name.lower()

    def _entry_reason(self, entry: Entry, part_number: str | None = None, fetch_reason: str | None = None) -> str | None:
        """Why an entry can't start now, or None (§3.2); the ⋯ menu and Settings ask it each time."""
        return unavailable_reason(entry, self._busy, self._source_root_state_name(), part_number, fetch_reason)

    def _document_update_hooks(self):
        from cockpit.ui.widgets.settings_dialog import DocumentUpdateHooks
        return DocumentUpdateHooks(
            check_unavailable=lambda: self._entry_reason(Entry.CHECK_ALL),
            reset_unavailable=lambda: self._entry_reason(Entry.RESET),
            last_run_line=self._last_run_line,
        )

    def _last_run_line(self) -> str:
        from cockpit.ui.document_update_settings import load_last_run
        from cockpit.services.update_run import last_run_line
        return last_run_line(load_last_run(self._settings))

    # --- Phase 55b section 5: update runs -----------------------------------

    def _run_services(self):
        from cockpit.ui.document_update_run import RunServices
        state, root_path = self._source_root_controller.source_root()
        return RunServices(
            repositories=self._bootstrapped.update_repositories,
            runtime_calc=self._bootstrapped.runtime_calc_svc,
            parsers=self._bootstrapped.parser_registry,
            source_root=root_path,
            storage_root=self._bootstrapped.config.file_storage_root,
            vocabulary=self.current_requirements_vocabulary(),
            requirements_refresh=lambda audit_ids: self._requirements_reconciler.trigger(),
            orphan=self._orphan_check_worker,
        )

    def _start_update_run(self, keys, roles, confirm, operation: Operation, dialog_parent) -> bool:
        from cockpit.services.update_run import RunRequest
        from cockpit.ui.document_update_run import DocumentUpdateRun, new_run_id
        if self._busy.operation is not None or not keys:
            return False
        request = RunRequest(frozenset(roles), confirm, new_run_id())
        run = DocumentUpdateRun(request, keys, self._run_services(), dialog_parent=dialog_parent,
                                watchdog_ms=self._update_watchdog_ms, parent=self)
        run.finished.connect(self._on_update_run_finished)
        self._update_run = run
        self._begin(operation)
        run.start()
        return True

    def _on_check_all_requested(self) -> None:
        reason = self._entry_reason(Entry.CHECK_ALL)
        parent = self._settings_dialog or self
        if reason is not None:
            QMessageBox.information(parent, "Check for document updates", reason)
            return
        from cockpit.services.document_updates import PROBED_ROLES, open_family_keys
        from cockpit.services.update_run import ConfirmMode
        keys = open_family_keys(self._bootstrapped.audit_repo)
        if not keys:
            QMessageBox.information(parent, "Check for document updates", "There are no open audits to check.")
            return
        self._last_run_from_entry = True
        self._start_update_run(keys, PROBED_ROLES, ConfirmMode.PREVIEW, Operation.UPDATE_CHECK, parent)

    def _on_check_family_requested(self, audit_id: int) -> None:
        audit = self._bootstrapped.audit_repo.find_by_id(audit_id)
        if audit is None:
            return
        reason = self._entry_reason(Entry.CHECK_FAMILY, audit.part_number)
        if reason is not None:
            QMessageBox.information(self, "Check for document updates", reason)
            return
        from cockpit.services.document_updates import PROBED_ROLES, FamilyKey
        from cockpit.services.update_run import ConfirmMode
        self._last_run_from_entry = True
        self._start_update_run([FamilyKey(audit.part_number, audit.work_order_ref)], PROBED_ROLES,
                               ConfirmMode.PREVIEW, Operation.UPDATE_CHECK, self)

    def _on_update_run_finished(self, result) -> None:
        from cockpit.services.update_run import ConfirmMode, placements_check_message
        run = self._update_run
        self._update_run = None
        busy = self._busy.end()
        if result.abandoned:
            busy = busy.parked(True)
        self._set_busy(busy)
        if self._closing:
            return
        if result.request.confirm == ConfirmMode.PREVIEW and getattr(self, "_last_run_from_entry", False):
            from cockpit.ui.document_update_settings import store_last_run
            store_last_run(self._settings, result.summary)
            self.document_update_run_finished.emit()
        self._last_run_from_entry = False
        if result.applied_audits:
            self._refresh_after_update(result.applied_audits)
        if result.request.confirm == ConfirmMode.AUTOMATIC and run is not None:
            record = next(iter(result.records.values()), None)
            message = placements_check_message(record, result.stop)
            if message:
                self.toast.show_toast(message, "")
        if run is not None:
            run.deleteLater()

    def _refresh_after_update(self, applied_audits) -> None:
        """§5.7: the list repopulated, keeping its scroll; an open audit that was applied reloaded."""
        try:
            self.picker.populate(self._audit_read_svc.list_open(), keep_scroll=True)
        except Exception:
            logger.exception("Refreshing the Audit List after an update failed")
        current = self._audit_view.current_audit_id()
        if current is not None and current in applied_audits:
            try:
                self._audit_view.flush_pending_writes()
            except Exception:
                logger.exception("Flushing the open audit before its reload failed")
            self._audit_view.load(current)

    def _orphan_check_worker(self, thread, worker) -> None:
        """Phase 55 §7's parking: kept until it finishes; the checks wait, New audit doesn't (§5.6)."""
        entry = (thread, worker)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda e=entry: self._on_check_worker_unparked(e))
        self._orphaned_workers.append(entry)

    def _on_check_worker_unparked(self, entry) -> None:
        self._discard_orphan(entry)
        if not self._closing:
            self._set_busy(self._busy.parked(False))

    # --- Phase 55b section 9: the check after ingest -------------------------

    def _maybe_check_placements_after_ingest(self) -> None:
        latch = self._placements_latch
        if latch is None or latch.audit_id is None or not (latch.handler_done and latch.worker_done):
            return
        self._placements_latch = None
        self.check_placements_after_ingest(latch.audit_id)

    def check_placements_after_ingest(self, audit_id: int) -> None:
        """Brings a newly ingested family its placements, through the update run (§9)."""
        from cockpit.ingestion.roles import SourceRole
        from cockpit.services.document_updates import FamilyKey
        from cockpit.services.update_run import ConfirmMode
        if self._closing or self._source_root_state_name() != "configured":
            return
        if self._busy.parked_check:
            self.toast.show_toast(
                "Placements not read: the share hasn't answered an earlier check. "
                "Check for updates\u2026 will try again", "",
            )
            return
        if self._busy.operation is not None:
            return
        audit = self._bootstrapped.audit_repo.find_by_id(audit_id)
        if audit is None:
            return
        self._last_run_from_entry = False
        self._start_update_run([FamilyKey(audit.part_number, audit.work_order_ref)],
                               frozenset({SourceRole.PLACEMENTS}), ConfirmMode.AUTOMATIC,
                               Operation.PLACEMENTS_CHECK, None)

    def _on_drawing_fetch_succeeded(self, outcome) -> None:
        self._end_operation()
        self._fetch_watchdog.stop()
        self._fetch_thread.quit()
        self._fetch_thread.wait()
        self._fetch_worker.deleteLater()
        self._fetch_thread.deleteLater()
        self._fetch_thread = None
        self._fetch_worker = None

        audit_id, secondary = self._pending_drawing_target
        self._pending_drawing_target = None

        from cockpit.ingestion.locator import (
            LocatedDrawing, PendingDrawingSelection, as_pending_selection,
        )
        from cockpit.ingestion.roles import SourceRole

        pdf_path = None
        if isinstance(outcome, LocatedDrawing):
            pdf_path = outcome.pdf_path
        elif isinstance(outcome, PendingDrawingSelection):
            from cockpit.ui.widgets.fetch_dialog import SelectSourcesDialog
            dialog = SelectSourcesDialog(as_pending_selection(outcome), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                # A cancel is a decision, not a failure: nothing is copied,
                # nothing is registered, and no error is raised.
                return
            pdf_path = dialog.choices.get(SourceRole.PDF)

        if pdf_path is None:
            return

        if not secondary:
            # Phase 57 section 9.2: fitting a page can take half a minute, so the primary drawing
            # is copied, parsed and fitted on the drawing worker.
            self._start_drawing_update(audit_id, pdf_path)
            return

        try:
            service = self._bootstrapped.ingestion_service
            service.add_secondary_pdf_to_audit(audit_id, pdf_path)
        except Exception as e:
            logger.exception("Drawing fetch failed to attach")
            self._on_failed(FailurePayload.from_exception(e, "Could not attach the drawing"))
            return

        self._audit_view.load(audit_id)

    def _on_fetch_watchdog_timeout(self) -> None:
        self._abandon_fetch_worker()
        
        from cockpit.ingestion.errors import FetchTimedOut
        state, root_path = self._source_root_controller.source_root()
        err = FetchTimedOut(root_path, FETCH_WATCHDOG_TIMEOUT_MS)
        
        if hasattr(self, "_fetch_dialog") and self._fetch_dialog is not None:
            self._fetch_dialog.reject()
            
        self._on_failed(FailurePayload.from_exception(err, "Fetch timed out"))

    def _on_fetch_failed(self, payload: FailurePayload) -> None:
        self._end_operation()
        self._fetch_watchdog.stop()
        
        self._fetch_thread.quit()
        self._fetch_thread.wait()
        self._fetch_worker.deleteLater()
        self._fetch_thread.deleteLater()
        self._fetch_thread = None
        self._fetch_worker = None
        
        if hasattr(self, "_fetch_dialog") and self._fetch_dialog is not None:
            self._fetch_dialog.reject()
            
        self._on_failed(payload)

    def _on_fetch_succeeded(self, outcome) -> None:
        self._end_operation()
        self._fetch_watchdog.stop()
        
        self._fetch_thread.quit()
        self._fetch_thread.wait()
        self._fetch_worker.deleteLater()
        self._fetch_thread.deleteLater()
        self._fetch_thread = None
        self._fetch_worker = None
        
        if hasattr(self, "_fetch_dialog") and self._fetch_dialog is not None:
            self._fetch_dialog.accept()
            
        self._settings.setValue("ingestion/last_job_number", outcome.job_number)
        
        from cockpit.ingestion.locator import PendingSelection, LocatedFiles
        if isinstance(outcome, PendingSelection):
            from cockpit.ui.widgets.fetch_dialog import SelectSourcesDialog
            dialog = SelectSourcesDialog(outcome, self)
            if not dialog.exec():
                return
            
            from cockpit.ingestion.roles import SourceRole
            folder_set = next(
                (c for c in outcome.sets if c.role == SourceRole.ARTICLE_FOLDER), None
            )
            if folder_set is not None:
                # Section 4.1: a folder choice is upstream of role resolution --
                # which folder is chosen decides which files are candidates --
                # so it re-enters locate rather than resolve_selection.
                self._pending_article_folder = dialog.choices.get(SourceRole.ARTICLE_FOLDER)
                if self._pending_article_folder is None:
                    return
                _state, root_path = self._source_root_controller.source_root()
                self._start_job_fetch(outcome.job_number, root_path)
                return

            from cockpit.ingestion.locator import resolve_selection
            try:
                outcome = resolve_selection(outcome, dialog.choices)
            except Exception as e:
                self._on_failed(FailurePayload.from_exception(e, "Resolution failed"))
                return
                
        # Now we have LocatedFiles
        self._verify_and_ingest_located(outcome)

    def _verify_and_ingest_located(self, outcome) -> None:
        from cockpit.ingestion.filename_rules import derive_part_number_from_filename
        part_number = derive_part_number_from_filename(outcome.quartet.bom_path)
        
        open_audits = self._audit_read_svc.open_audits_for_part_number(part_number)
        if open_audits:
            msg = f"There are already {len(open_audits)} open audit(s) for part number {part_number}:\n\n"
            for a in open_audits:
                msg += f"• WO: {a.work_order_ref}"
                if a.split_suffix:
                    msg += f"{a.split_suffix}"
                msg += "\n"
            msg += "\nAre you sure you want to ingest another copy?"
            
            reply = QMessageBox.question(self, "Duplicate Part Number", msg, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if reply == QMessageBox.StandardButton.No:
                return
                
        paths = []
        if outcome.quartet.bom_path: paths.append(outcome.quartet.bom_path)
        if outcome.quartet.traveler_path: paths.append(outcome.quartet.traveler_path)
        if outcome.quartet.notes_path: paths.append(outcome.quartet.notes_path)
        if outcome.quartet.pdf_path: paths.append(outcome.quartet.pdf_path)
        
        plan = self._run_ingest_preflight(outcome.quartet.traveler_path, outcome)
        if plan is _PREFLIGHT_ABORTED:
            return
        self._on_drop_received(paths, plan=plan)

    def _run_ingest_preflight(self, traveler_path, outcome=None):
        """Settles every question that needs a human before the worker starts.

        pre:  nothing has been copied and nothing written
        post: returns an IngestionPlan, or _PREFLIGHT_ABORTED when the operator
              declined; by the time ingest() runs there is no further input to
              wait for, which is what keeps a dialog out of the savepoint

        Order matters: the article designation is known from the mode, the
        quantity prompt is prefilled from the probe, and the replacement
        confirmation comes last because it is the only irreversible one.
        """
        from cockpit.ingestion.service import IngestionPlan, probe_identity
        from cockpit.services.ingestion_mode import IngestionMode

        mode = getattr(self, "_pending_mode", None) or IngestionMode.STANDARD
        article = "FA"
        if mode is IngestionMode.ARTICLE_REVISION and outcome is not None:
            from cockpit.ingestion.filename_rules import article_designation
            article = article_designation(outcome.job_directory.name) or "FA"

        service = self._bootstrapped.ingestion_service
        try:
            probed = probe_identity(traveler_path, service.coord_map)
        except Exception as e:
            logger.exception("Identity pre-flight failed")
            self._on_failed(FailurePayload.from_exception(e, "Could not read the traveler"))
            return _PREFLIGHT_ABORTED

        quantity_override = None
        if mode.prompts_for_quantity:
            from PyQt6.QtWidgets import QInputDialog
            value, accepted = QInputDialog.getInt(
                self, "Job quantity",
                "Documents for an article revision are often out of date.\n"
                "Confirm or correct the job quantity:",
                probed.quantity, 1, 1_000_000,
            )
            if not accepted:
                return _PREFLIGHT_ABORTED
            if value != probed.quantity:
                quantity_override = value

        family = service.audit_repo.list_family(probed.part_number, probed.work_order_ref)
        replace_ids = ()
        if family:
            from cockpit.services.replacement import summarise_family
            from cockpit.ui.widgets.replace_confirm_dialog import ReplaceConfirmDialog

            casualties = summarise_family(family, service.tht_repo)
            dialog = ReplaceConfirmDialog(
                probed.part_number, probed.work_order_ref, article, casualties, self
            )
            if not dialog.exec():
                # A decision, not a failure: nothing copied, nothing written.
                return _PREFLIGHT_ABORTED
            replace_ids = tuple(a.id for a in family)

        return IngestionPlan(
            expected_identity=probed,
            replace_family=replace_ids,
            article_revision=article,
            quantity_override=quantity_override,
        )

    def _on_holidays_requested(self) -> None:
        from cockpit.ui.widgets.holiday_dialog import HolidayDialog
        dialog = HolidayDialog(self._holiday_svc, self)
        dialog.exec()
        self._reload_list()

    def _on_overview_second_ops_requested(self) -> None:
        if self._second_ops_settings_controller is None:
            return
            
        from cockpit.ui.widgets.second_ops_dialog import SecondOpsOverviewDialog
        dialog = SecondOpsOverviewDialog(
            self._bootstrapped.audit_bom_component_repo,
            self._second_ops_settings_controller,
            self._settings,
            self
        )
        if dialog.exec():
            audit_id = dialog.chosen_audit_id()
            dialog.deleteLater()
            if audit_id is not None:
                self._on_picker_audit_selected(audit_id)
                epoch = self._load_epoch

                def do_open_second_ops():
                    if epoch != self._load_epoch:
                        return
                    self._on_audit_second_ops_requested(audit_id)

                QTimer.singleShot(0, do_open_second_ops)
        else:
            dialog.deleteLater()

    def _unregister_review_dialog(self, audit_id: int, serial: int) -> None:
        entry = self._second_ops_review_dialogs.get(audit_id)
        if entry is not None and entry[0] == serial:
            self._second_ops_review_dialogs.pop(audit_id)

    def _on_audit_second_ops_requested(self, audit_id: int) -> None:
        if self._second_ops_settings_controller is None:
            return
            
        entry = self._second_ops_review_dialogs.get(audit_id)
        if entry is not None:
            dialog = entry[1]
            dialog.raise_()
            dialog.activateWindow()
            return

        from cockpit.ui.widgets.second_ops_dialog import SecondOpsAuditDialog
        dialog = SecondOpsAuditDialog(
            audit_id,
            self._bootstrapped.source_file_repo,
            self._second_ops_settings_controller,
            self._settings,
            self
        )
        serial = self._next_registration_serial
        self._next_registration_serial += 1
        self._second_ops_review_dialogs[audit_id] = (serial, dialog)
        
        dialog.finished.connect(lambda result_code, a=audit_id, s=serial: self._unregister_review_dialog(a, s))
        dialog.destroyed.connect(lambda obj=None, a=audit_id, s=serial: self._unregister_review_dialog(a, s))
        dialog.show()

    def _unregister_cleaning_report_dialog(self, audit_id: int, serial: int) -> None:
        entry = self._cleaning_report_dialogs.get(audit_id)
        if entry is not None and entry[0] == serial:
            self._cleaning_report_dialogs.pop(audit_id)

    def _on_audit_cleaning_report_requested(self, audit_id: int) -> None:
        entry = self._cleaning_report_dialogs.get(audit_id)
        if entry is not None:
            dialog = entry[1]
            dialog.raise_()
            dialog.activateWindow()
            return

        from cockpit.ui.widgets.cleaning_dialogs import CleaningReportDialog
        dialog = CleaningReportDialog(
            audit_id,
            self._bootstrapped.source_file_repo,
            self,
        )
        serial = self._next_registration_serial
        self._next_registration_serial += 1
        self._cleaning_report_dialogs[audit_id] = (serial, dialog)

        dialog.finished.connect(lambda result_code, a=audit_id, s=serial: self._unregister_cleaning_report_dialog(a, s))
        dialog.destroyed.connect(lambda obj=None, a=audit_id, s=serial: self._unregister_cleaning_report_dialog(a, s))
        dialog.show()

    def _on_drop_received(self, paths: list[pathlib.Path], plan=None) -> None:
        if self._operation_in_flight:
            return

        self._pending_plan = plan
        
        self._begin(Operation.INGEST)
        self._placements_latch = _IngestLatch()
        self.drop_area.setEnabled(False)
        self.progress_view.reset()
        self.stacked.setCurrentWidget(self.progress_view)
        
        self._thread = QThread()
        self._worker = IngestionWorker(
            self._bootstrapped.ingestion_service,
            paths,
            plan=self._pending_plan,
            requirements_vocabulary=self.current_requirements_vocabulary(),
        )
        self._worker.moveToThread(self._thread)
        
        self._thread.started.connect(self._worker.run)

        # Cleanup is connected BEFORE the handlers below, and the order is
        # load-bearing: Qt invokes slots in connection order and stops the
        # emission if one raises. With cleanup last, an exception in
        # _on_ingest_succeeded left the thread running, _operation_in_flight
        # stuck true and the progress view on screen forever -- a silent hang
        # rather than a reported error. Cleanup first makes the terminal
        # transition unconditional.
        self._worker.succeeded_signal.connect(self._thread.quit)
        self._worker.failed_signal.connect(self._thread.quit)
        self._worker.cancelled_signal.connect(self._thread.quit)

        self._worker.progress_signal.connect(self._on_progress)
        self._worker.succeeded_signal.connect(self._on_ingest_succeeded)
        self._worker.failed_signal.connect(self._on_failed)
        self._worker.cancelled_signal.connect(self._on_cancelled)

        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_worker_finished)

        # Phase 32 (3.1): run the CPU-bound ingest at low priority so the
        # UI thread keeps getting scheduled (GIL starvation mitigation).
        self._thread.start(QThread.Priority.LowPriority)

    def _on_cancel_requested(self) -> None:
        if self._worker:
            self._worker.request_cancel()

    def _on_progress(self, stage_str: str, annotation: str | None = None) -> None:
        try:
            stage = ProgressStage(stage_str)
            self.progress_view.advance(stage, annotation)
        except ValueError:
            logger.exception('Exception caught in main_window')
            pass

    def _on_ingest_succeeded(self, summary: AuditSummary) -> None:
        if self._placements_latch is not None:
            self._placements_latch.audit_id = summary.audit_id
        self._on_ingest_succeeded_body(summary)
        if self._placements_latch is not None:
            self._placements_latch.handler_done = True
        self._maybe_check_placements_after_ingest()

    def _on_ingest_succeeded_body(self, summary: AuditSummary) -> None:
        # Before load(), which hands the BOM lines to whatever segment
        # bind_library created. Without this a freshly ingested job showed no
        # Library segment while the same job reopened from the picker did.
        self._sync_mpn_library_state()
        self._audit_view.load(summary.audit_id)
        self.stacked.setCurrentWidget(self._audit_view)
        self.toast.show_success(summary)
        open_full_report = False
        if summary.cleaning and summary.cleaning.consequential:
            from cockpit.ui.widgets.cleaning_dialogs import CleaningChangesDialog
            changes_dialog = CleaningChangesDialog(summary.cleaning, self)
            changes_dialog.exec()
            open_full_report = changes_dialog.full_report_requested()
        self._offer_split_after_ingest(summary.audit_id)
        if open_full_report:
            self._on_audit_cleaning_report_requested(summary.audit_id)

    def _offer_split_after_ingest(self, audit_id: int) -> None:
        """Phase 51 section 5: Split mode runs the shipped SplitDialog.

        post: on success the view reloads split; on failure or cancellation the
              audit is kept and the outcome is surfaced as "ingested, not split"

        The dialog opens against the persisted audit with its authoritative
        quantity, so no parameters-only variant and no duplicated validation is
        needed. The ingest and the split are two transactions: rolling the
        ingest back would discard a correct result to undo an unrelated
        decision.
        """
        from cockpit.services.ingestion_mode import IngestionMode

        mode = getattr(self, "_pending_mode", None)
        self._pending_mode = IngestionMode.STANDARD
        self._pending_plan = None
        if mode is None or not mode.splits_after_ingest:
            return

        view = self._audit_view._session.current_view()
        if view is None:
            self.toast.show_toast("Ingested, not split", "Could not open the new audit")
            return

        from cockpit.ui.widgets.split_dialog import SplitDialog
        try:
            dialog = SplitDialog(view, self._bootstrapped.split_svc, self)
            if dialog.exec() and dialog.outcome:
                self._audit_view.load(audit_id)
                return
        except Exception:
            logger.exception("Split after ingest failed")

        self.toast.show_toast(
            "Ingested, not split",
            "Use Split from the audit's ⋯ menu to divide it",
        )

    def _on_dashboard_exit(self) -> None:
        self._reload_list()

    def _reload_list(self) -> None:
        # Optimize06 section 3.6: _reload_list and _show_drop_area are the two
        # exit-to-list paths, and the release belongs on the path rather than on
        # one chosen caller. setCurrentWidget only hides. Placing it at
        # _on_dashboard_exit instead would leave _on_midnight_timer -- which
        # reaches here without the back button -- switching away from a still
        # loaded audit view, which is F2 recurring on that path.
        self._audit_view.unload()
        open_audits = self._audit_read_svc.list_open()
        if not open_audits:
            self._show_drop_area()
        else:
            self.picker.populate(open_audits)
            self.stacked.setCurrentWidget(self.picker)

    def _on_failed(self, payload: FailurePayload) -> None:
        # We only go to drop area if we were doing ingest
        if self.stacked.currentWidget() == self.progress_view:
            self._show_drop_area()
        dialog = ErrorDialog(payload, self)
        dialog.exec()

    def _on_cancelled(self) -> None:
        self._show_drop_area()
        self.toast.show_cancel()

    def _on_worker_finished(self) -> None:
        self._end_operation()
        latch = self._placements_latch
        if latch is not None:
            latch.worker_done = True
            if latch.audit_id is None:
                self._placements_latch = None
        self._worker = None
        self._thread = None
        self.drop_area.setEnabled(True)

        if self._close_requested:
            self.close()
            return
        # Phase 54 section 7.2: scans the ingest's transaction kept out are
        # written now, rather than at the next idle pass.
        self._requirements_reconciler.resume_if_deferred()
        self._maybe_check_placements_after_ingest()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._closed:
            event.accept()
            return
        # Phase 55b §3.3: only an ingest asks. A fetch, a drawing update or a check is ended without
        # asking, since none of them writes before its apply on the UI thread. _closing is set first,
        # so the handlers those endings run skip their refreshes and the last-run line.
        if self._busy.operation != Operation.INGEST:
            self._closing = True
        if self._busy.operation in (Operation.JOB_FETCH, Operation.DRAWING_FETCH):
            self._abandon_fetch_worker()
            fetch_dialog = getattr(self, "_fetch_dialog", None)
            if fetch_dialog is not None:
                try:
                    fetch_dialog.reject()
                except RuntimeError:
                    pass
        if self._drawing_worker is not None:
            self._abandon_drawing_worker()
        if self._update_run is not None and self._update_run.active:
            self._update_run.shutdown_for_close()
        if self._busy.operation == Operation.INGEST:
            event.ignore()
            confirmed = QMessageBox.question(
                self, 
                "Cancel ingest?", 
                "An ingest is currently in flight. Are you sure you want to cancel and exit?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirmed == QMessageBox.StandardButton.Yes:
                self._close_requested = True
                if self._worker:
                    self._worker.request_cancel()
        else:
            self._closing = True
            if self._settings_dialog is not None:
                try:
                    self._settings_dialog.reject()
                except RuntimeError:
                    pass
            for serial, dialog in list(self._cleaning_report_dialogs.values()):
                dialog.close()
            for serial, dialog in list(self._second_ops_review_dialogs.values()):
                dialog.close()
            for serial, dialog in list(self._placements_report_dialogs.values()):
                dialog.close()

            try:
                self._audit_view.flush_pending_writes()
            except Exception:
                logger.exception('Exception caught in main_window')
                pass

            self._audit_view.unload()
            # Stops the requirements reconciler too, before the connection
            # closes (Phase 54 section 7.2).
            self.shutdown_render_thread()

            from cockpit.persistence.connection import run_idle_maintenance
            run_idle_maintenance(self._bootstrapped.conn)
            self._bootstrapped.conn.close()
            self._closed = True
            self._hand_orphans_to_cpp()
            event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast._position_bottom_right()

    def shutdown_render_thread(self, timeout_ms: int = RENDER_SHUTDOWN_TIMEOUT_MS) -> bool:
        """
        Intent: Stop the render thread without hanging the exit path on an
                in-flight rasterize, and the requirements reconciler with it
                (Phase 54 section 7.2), so every caller that tears a window
                down stops both.
        Pre:    timeout_ms > 0.
        Post:   Returns True if the render thread exited within timeout_ms. On
                False the thread is left running, the caller still accepts the
                close event, and the condition is logged at WARNING -- a still-
                rasterizing worker holds no DB handle and no widget reference,
                so process exit is safe. The reconciler follows the same
                contract under its own bound.
        Raises: Nothing.
        """
        self._audit_view.set_render_worker_alive(False)
        if hasattr(self, "_requirements_reconciler"):
            self._requirements_reconciler.shutdown(timeout_ms)
        if getattr(self, "_render_thread", None) is None:
            return True

        self._render_thread.quit()
        exited = self._render_thread.wait(timeout_ms)
        if not exited:
            # Deliberately NOT terminate(): killing a thread inside fitz can
            # leave the allocator inconsistent, and the second wait() that
            # follows terminate() is unbounded -- F9 reintroduced.
            logger.warning("render thread did not exit within %d ms; exiting anyway", timeout_ms)
        return exited
