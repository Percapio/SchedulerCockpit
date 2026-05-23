"""Main window."""

import pathlib
from PyQt6.QtCore import Qt, QThread
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QMainWindow, QStackedWidget, QMessageBox, QApplication

from cockpit.ingestion.progress import ProgressStage
from cockpit.ui.bootstrap import BootstrappedApp
from cockpit.ui.error_messages import FailurePayload
from cockpit.ui.widgets import (
    DropArea, ProgressView, Toast, ErrorDialog,
    OpenAuditPicker, Dashboard
)
from cockpit.ui.workers import IngestionWorker, AuditSummary

from cockpit.services.audit_read import AuditReadService
from cockpit.services.checklist import ChecklistService
from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService
from cockpit.services.audit_metadata import AuditMetadataService


class MainWindow(QMainWindow):
    def __init__(
        self,
        app: QApplication,
        bootstrapped_app: BootstrappedApp,
        audit_read_svc: AuditReadService,
        checklist_svc: ChecklistService,
        split_svc: AuditSplitService,
        completion_svc: CompletionService,
        audit_metadata_svc: AuditMetadataService
    ) -> None:
        super().__init__()
        self._app = app
        self._bootstrapped = bootstrapped_app
        self._audit_read_svc = audit_read_svc
        
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
        self.stacked.addWidget(self.drop_area)
        
        stages = list(ProgressStage)
        self.progress_view = ProgressView(stages)
        self.progress_view.cancel_requested.connect(self._on_cancel_requested)
        self.stacked.addWidget(self.progress_view)
        
        self.picker = OpenAuditPicker()
        self.picker.audit_selected.connect(self._on_picker_audit_selected)
        self.picker.new_audit_requested.connect(self._on_picker_new_audit_requested)
        self.stacked.addWidget(self.picker)
        
        self.dashboard = Dashboard(checklist_svc, split_svc, completion_svc, audit_metadata_svc)
        self.dashboard.exit_requested.connect(self._on_dashboard_exit)
        self.dashboard.error_occurred.connect(self._on_failed)
        self.stacked.addWidget(self.dashboard)
        
        self.toast = Toast(self)
        
        self._resolve_initial_page()
        
    def _resolve_initial_page(self) -> None:
        open_audits = self._audit_read_svc.list_open()
        if not open_audits:
            self.stacked.setCurrentWidget(self.drop_area)
        elif len(open_audits) == 1:
            self.dashboard.load(open_audits[0].audit_id)
            self.stacked.setCurrentWidget(self.dashboard)
        else:
            self.picker.populate(open_audits)
            self.stacked.setCurrentWidget(self.picker)
            
    def _on_picker_audit_selected(self, audit_id: int) -> None:
        self.dashboard.load(audit_id)
        self.stacked.setCurrentWidget(self.dashboard)
        
    def _on_picker_new_audit_requested(self) -> None:
        self.stacked.setCurrentWidget(self.drop_area)

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
        
        self._thread.start()

    def _on_cancel_requested(self) -> None:
        if self._worker:
            self._worker.request_cancel()

    def _on_progress(self, stage_str: str) -> None:
        try:
            stage = ProgressStage(stage_str)
            self.progress_view.advance(stage)
        except ValueError:
            pass

    def _on_ingest_succeeded(self, summary: AuditSummary) -> None:
        self.dashboard.load(summary.audit_id)
        self.stacked.setCurrentWidget(self.dashboard)
        self.toast.show_success(summary)

    def _on_dashboard_exit(self) -> None:
        self._resolve_initial_page()

    def _on_failed(self, payload: FailurePayload) -> None:
        # We only go to drop area if we were doing ingest
        if self.stacked.currentWidget() == self.progress_view:
            self.stacked.setCurrentWidget(self.drop_area)
        dialog = ErrorDialog(payload, self)
        dialog.exec()

    def _on_cancelled(self) -> None:
        self.stacked.setCurrentWidget(self.drop_area)
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
            self._bootstrapped.conn.close()
            event.accept()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.toast.isVisible():
            self.toast._position_bottom_right()
