"""Dashboard widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSplitter
)

from cockpit.services.checklist import ChecklistService
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.services.split import AuditSplitService
from cockpit.services.audit_metadata import AuditMetadataService
from cockpit.services.views import ActiveAuditView, ChecklistRowKey, SelectionIntent, SelectionKind, ChecklistRowKind
from cockpit.ingestion.service import IngestionService
from cockpit.persistence.types import AuditStatus
from cockpit.persistence.errors import PersistenceError, IncompleteChecklistError, IllegalStateTransition
from cockpit.ui.error_messages import render
from cockpit.ui.widgets.add_drawing_dialog import AddDrawingDialog

from .identity_header import IdentityHeader
from .checklist_view import ChecklistView
from .split_dialog import SplitDialog
from cockpit.ui.theme import Theme
import logging
logger = logging.getLogger(__name__)



_METADATA_DISPLAY_LABELS: dict[str, str] = {
    "customer_name": "Customer",
    "sales_order_number": "S/O",
    "lead_time_days": "LT",
    "release_date": "Release",
}


class Dashboard(QWidget):
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)
    reload_requested = pyqtSignal(int)
    
    tht_body_clicked = pyqtSignal(object)
    tht_mpn_clicked = pyqtSignal(object)
    empty_clicked = pyqtSignal()
    esc_pressed = pyqtSignal()

    def __init__(
        self,
        checklist_service: ChecklistService,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        audit_metadata_service: AuditMetadataService,
        ingestion_service: IngestionService,
        theme: Theme,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._checklist_service = checklist_service
        self._split_service = split_service
        self._completion_service = completion_service
        self._audit_metadata_service = audit_metadata_service
        self._ingestion_service = ingestion_service
        self._view: ActiveAuditView | None = None
        self._current_audit_id: int | None = None
        
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc_shortcut.activated.connect(self.esc_pressed.emit)
        
        layout = QVBoxLayout(self)
        
        self.header = IdentityHeader()
        self.header.back_requested.connect(self._on_back_requested)
        self.header.ship_date_commit_requested.connect(self._on_ship_date_commit)
        layout.addWidget(self.header)
        
        self.metadata_band = QWidget()
        self.metadata_layout = QHBoxLayout(self.metadata_band)
        self.metadata_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.metadata_band)
        
        self._checklist_splitter = QSplitter(Qt.Orientation.Vertical)
        self._checklist_splitter.setChildrenCollapsible(False)
        layout.addWidget(self._checklist_splitter, stretch=1)
        
        self.checklist_tht = ChecklistView(self._theme)
        self.checklist_tht.toggle_requested.connect(self._on_row_toggle)
        self.checklist_tht.body_clicked.connect(self.tht_body_clicked.emit)
        self.checklist_tht.mpn_clicked.connect(self.tht_mpn_clicked.emit)
        self.checklist_tht.empty_space_clicked.connect(self.empty_clicked.emit)
        self.checklist_tht.setMinimumHeight(80)
        self._checklist_splitter.addWidget(self.checklist_tht)
        
        self.checklist_notes = ChecklistView(self._theme)
        self.checklist_notes.toggle_requested.connect(self._on_row_toggle)
        self.checklist_notes.empty_space_clicked.connect(self.empty_clicked.emit)
        self.checklist_notes.setMinimumHeight(80)
        self._checklist_splitter.addWidget(self.checklist_notes)
        
        self._checklist_splitter.setSizes([600, 300]) # default ratio
        
        footer = QHBoxLayout()
        self.split_btn = QPushButton("Split")
        self.split_btn.clicked.connect(self._on_split_clicked)
        footer.addWidget(self.split_btn)
        
        self.add_drawing_btn = QPushButton("Add Drawing")
        self.add_drawing_btn.clicked.connect(self._on_add_drawing_clicked)
        footer.addWidget(self.add_drawing_btn)
        
        footer.addStretch()
        
        self.verify_all_btn = QPushButton("Verify All")
        self.verify_all_btn.clicked.connect(self._on_verify_all_clicked)
        footer.addWidget(self.verify_all_btn)
        
        self.complete_btn = QPushButton("Mark Complete")
        self.complete_btn.clicked.connect(self._on_complete_clicked)
        footer.addWidget(self.complete_btn)
        
        layout.addLayout(footer)

    def load(self, audit_id: int) -> None:
        self._current_audit_id = audit_id
        try:
            self._view = self._checklist_service.load_active_audit(audit_id)
            self._apply_view()
        except Exception as e:
            logger.exception('Exception caught in dashboard')
            self.error_occurred.emit(render(e))

    def reload(self) -> None:
        if self._current_audit_id is not None:
            self.load(self._current_audit_id)

    def _apply_view(self) -> None:
        if self._view is None:
            return
            
        self.header.set_audit(self._view)
        
        # Build metadata band
        while self.metadata_layout.count():
            item = self.metadata_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
                
        metadata = self._view.traveler_metadata or {}
        for key, label in _METADATA_DISPLAY_LABELS.items():
            val = metadata.get(key, "—")
            self.metadata_layout.addWidget(QLabel(f"{label}: {val}"))
            
        self.checklist_tht.populate_section(self._view.tht_rows, f"Through-Hole - Unique MPNs: {len(self._view.tht_rows)} | Total Placements: {self._view.tht_placement_count}")
        self.checklist_notes.populate_section(self._view.notes_rows, f"Build Notes ({len(self._view.notes_rows)} items)")
        self._refresh_add_drawing_btn_label(self._view)
        self._update_enablement()

    def _refresh_add_drawing_btn_label(self, view: ActiveAuditView) -> None:
        if view.has_pdf:
            self.add_drawing_btn.setText("Replace Drawing")
        else:
            self.add_drawing_btn.setText("Add Drawing")

    def flush_audit_notes(self) -> None:
        pass

    def _on_back_requested(self) -> None:
        self.flush_audit_notes()
        self.exit_requested.emit()

    def _update_enablement(self) -> None:
        if self._view is None:
            return
            
        if self._view.status == AuditStatus.COMPLETED:
            self.checklist_tht.setEnabled(False)
            self.checklist_notes.setEnabled(False)
            self.split_btn.setEnabled(False)
            self.verify_all_btn.setEnabled(False)
            self.complete_btn.setEnabled(False)
            self.header.ship_date_fld.setEnabled(False)
        else:
            self.checklist_tht.setEnabled(True)
            self.checklist_notes.setEnabled(True)
            self.split_btn.setEnabled(True)
            self.verify_all_btn.setEnabled(not self._view.is_fully_verified)
            self.complete_btn.setEnabled(self._view.is_fully_verified)
            self.header.ship_date_fld.setEnabled(True)



    def _on_row_toggle(self, row_key: ChecklistRowKey, new_state: bool) -> None:
        try:
            updated = self._checklist_service.set_verification(
                row_key, new_state
            )
            self._view = self._view.with_row_replaced(updated)
            if row_key.kind == "tht":
                self.checklist_tht.update_row(updated)
            else:
                self.checklist_notes.update_row(updated)
            self._update_enablement()
        except PersistenceError as exc:
            logger.exception('Exception caught in dashboard')
            if row_key.kind == "tht":
                self.checklist_tht.revert_row(row_key)
            else:
                self.checklist_notes.revert_row(row_key)
            self.reload()
            self.error_occurred.emit(render(exc))

    def _on_split_clicked(self) -> None:
        if self._view is None:
            return
            
        dialog = SplitDialog(self._view, self._split_service, self)
        try:
            if dialog.exec():
                if dialog.outcome:
                    self.reload()
                    # We need to show toast. The simplest way is to emit a signal, or wait:
                    # In Phase 3, toast is on MainWindow. Phase 4 says "DASH->>MW: show Toast".
                    # Let's add a signal for it.
                    # Or we can just access main_window via self.window().
                    win = self.window()
                    if hasattr(win, "toast"):
                        win.toast.show_toast(f"Split into {dialog.outcome.sibling_suffix} (qty {dialog.outcome.sibling_quantity})", "")
        except Exception as e:
            logger.exception('Exception caught in dashboard')
            self.error_occurred.emit(render(e))

    def _on_add_drawing_clicked(self) -> None:
        if self._view is None:
            return
        from PyQt6.QtWidgets import QDialog
        dialog = AddDrawingDialog(self._ingestion_service, self._view.audit_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_requested.emit(self._view.audit_id)

    def _on_complete_clicked(self) -> None:
        if self._current_audit_id is None:
            return
        try:
            outcome = self._completion_service.complete_and_cleanup(self._current_audit_id)
            win = self.window()
            if hasattr(win, "toast"):
                win.toast.show_toast(f"Completed and cleaned up", "")
            self.exit_requested.emit()
        except (IncompleteChecklistError, IllegalStateTransition) as exc:
            logger.exception('Exception caught in dashboard')
            payload = render(exc)
            self.error_occurred.emit(payload)
            self.reload()
        except CleanupFailedError as exc:
            logger.exception('Exception caught in dashboard')
            payload = render(exc)
            self.error_occurred.emit(payload)
            self.exit_requested.emit()
        except PersistenceError as exc:
            logger.exception('Exception caught in dashboard')
            payload = render(exc)
            self.error_occurred.emit(payload)
            self.reload()

    def _on_verify_all_clicked(self) -> None:
        if self._view is None:
            return
            
        self.verify_all_btn.setEnabled(False)
        self.complete_btn.setEnabled(False)
        try:
            reloaded = self._checklist_service.verify_all(self._view.audit_id)
            self._view = reloaded
            self.checklist_tht.populate_section(reloaded.tht_rows, f"THT Verification ({len(reloaded.tht_rows)} items)")
            self.checklist_notes.populate_section(reloaded.notes_rows, f"Build Notes ({len(reloaded.notes_rows)} items)")
            self._update_enablement()
        except PersistenceError as exc:
            logger.exception('Exception caught in dashboard')
            self.reload()
            payload = render(exc)
            self.error_occurred.emit(payload)

    def _on_ship_date_commit(self, new_value) -> None:
        if self._view is None:
            return
            
        try:
            self._audit_metadata_service.set_ship_date(self._view.audit_id, new_value)
            self._view = self._view.with_ship_date(new_value)
            self.header.set_audit(self._view)
        except PersistenceError as exc:
            logger.exception('Exception caught in dashboard')
            self.header.ship_date_revert()
            self.reload()
            payload = render(exc)
            self.error_occurred.emit(payload)


