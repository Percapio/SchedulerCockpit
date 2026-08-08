"""Main window."""

import pathlib
from datetime import datetime, timedelta
from PyQt6.QtCore import Qt, QThread, QSettings, QTimer, QEvent
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QApplication

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
import logging
logger = logging.getLogger(__name__)

# Sized against one worst-case single-page rasterize at unscaled 4K (~3.6 s per
# Patch01 section 8.2). Beyond this the worker is wrong, not slow, and the exit
# path stops waiting on it.
RENDER_SHUTDOWN_TIMEOUT_MS = 10_000


class MainWindow(QMainWindow):
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
        runtime_settings_controller=None
    ) -> None:
        super().__init__()
        self._theme = theme
        self._app = app
        self._bootstrapped = bootstrapped_app
        self._audit_read_svc = audit_read_svc
        self._holiday_svc = holiday_svc
        
        self.setWindowTitle("Local Audit & Routing Checklist Utility")
        self.resize(800, 600)
        
        self._worker_in_flight = False
        self._close_requested = False
        self._worker = None
        self._thread = None
        
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
        self.picker.holidays_requested.connect(self._on_holidays_requested)
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
            theme=self._theme
        )
        self._audit_view.exit_requested.connect(self._on_dashboard_exit)
        self._audit_view.error_occurred.connect(self._on_failed)
        self._audit_view.ops_per_board_change_requested.connect(self._on_ops_per_board_change_requested)
        self.stacked.addWidget(self._audit_view)
        
        self.toast = Toast(self)

        self._style_controller = style_controller
        self._runtime_settings_controller = runtime_settings_controller
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
        
        def on_scale_changed(pt: int) -> None:
            pct = int(round(pt / self._font_scale._bounds.default_pt * 100))
            self._audit_view.apply_font_scale(pct)
            
        self._font_scale.scale_changed.connect(on_scale_changed)
        self._audit_view.font_scale_change_requested.connect(self._font_scale.request_delta)
        self.picker.font_scale_change_requested.connect(self._font_scale.request_delta)
        on_scale_changed(self._font_scale.current_pt())
        
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
        
    def _on_runtime_constants_changed(self) -> None:
        self._runtime_constants_dirty = True

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
                self._bootstrapped.config,
                self
            )
            dialog.exec()
        finally:
            if self._runtime_constants_dirty and self._runtime_settings_controller is not None:
                self._recompute_and_refresh()

    def _on_style_changed(self) -> None:
        # Preset/font change: regenerate stylesheet at the current scale and
        # repaint the list so semantic column colors pick up the new palette.
        self._font_scale.reapply()
        self.picker.table_view.viewport().update()

    def _run_idle_maintenance(self) -> None:
        from cockpit.persistence.connection import run_idle_maintenance
        run_idle_maintenance(self._bootstrapped.conn)

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
        self._audit_view.show_loading_placeholder()
        self.stacked.setCurrentWidget(self._audit_view)
        
        def do_load():
            if epoch != self._load_epoch:
                return
            self._audit_view.load(audit_id)
            
        QTimer.singleShot(0, do_load)
        
    def _on_complete_requested(self, audit_id: int) -> None:
        if not confirm_destructive(
            title="Complete audit",
            body="This permanently deletes the audit and its source files. This cannot be undone.",
            confirm_label="Complete"
        ):
            return
            
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
        
    def _on_picker_new_audit_requested(self) -> None:
        self._show_drop_area()

    def _on_holidays_requested(self) -> None:
        from cockpit.ui.widgets.holiday_dialog import HolidayDialog
        dialog = HolidayDialog(self._holiday_svc, self)
        dialog.exec()
        self._reload_list()

    def _on_drop_received(self, paths: list[pathlib.Path]) -> None:
        if self._worker_in_flight:
            return
            
        self._worker_in_flight = True
        self.drop_area.setEnabled(False)
        self.progress_view.reset()
        self.stacked.setCurrentWidget(self.progress_view)
        
        self._thread = QThread()
        self._worker = IngestionWorker(self._bootstrapped.ingestion_service, paths)
        self._worker.moveToThread(self._thread)
        
        self._thread.started.connect(self._worker.run)
        
        self._worker.progress_signal.connect(self._on_progress)
        self._worker.succeeded_signal.connect(self._on_ingest_succeeded)
        self._worker.failed_signal.connect(self._on_failed)
        self._worker.cancelled_signal.connect(self._on_cancelled)
        
        # Cleanup
        self._worker.succeeded_signal.connect(self._thread.quit)
        self._worker.failed_signal.connect(self._thread.quit)
        self._worker.cancelled_signal.connect(self._thread.quit)
        
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
        self._audit_view.load(summary.audit_id)
        self.stacked.setCurrentWidget(self._audit_view)
        self.toast.show_success(summary)

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
        self._worker_in_flight = False
        self._worker = None
        self._thread = None
        self.drop_area.setEnabled(True)
        
        if self._close_requested:
            self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._worker_in_flight:
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
            try:
                self._audit_view.flush_pending_writes()
            except Exception:
                logger.exception('Exception caught in main_window')
                pass

            self._audit_view.unload()
            self.shutdown_render_thread()
                
            from cockpit.persistence.connection import run_idle_maintenance
            run_idle_maintenance(self._bootstrapped.conn)
            self._bootstrapped.conn.close()
            event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast._position_bottom_right()

    def shutdown_render_thread(self, timeout_ms: int = RENDER_SHUTDOWN_TIMEOUT_MS) -> bool:
        """
        Intent: Stop the render thread without hanging the exit path on an
                in-flight rasterize.
        Pre:    timeout_ms > 0.
        Post:   Returns True if the thread exited within timeout_ms. On False
                the thread is left running, the caller still accepts the close
                event, and the condition is logged at WARNING -- a still-
                rasterizing worker holds no DB handle and no widget reference,
                so process exit is safe.
        Raises: Nothing.
        """
        self._audit_view.set_render_worker_alive(False)
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
