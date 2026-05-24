"""Dashboard widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel
)

from cockpit.services.checklist import ChecklistService
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.services.split import AuditSplitService
from cockpit.services.audit_metadata import AuditMetadataService
from cockpit.services.views import ActiveAuditView, ChecklistRowKey, SelectionIntent, SelectionKind, ChecklistRowKind
from cockpit.persistence.types import AuditStatus
from cockpit.persistence.errors import PersistenceError, IncompleteChecklistError, IllegalStateTransition
from cockpit.ui.error_messages import render

from .identity_header import IdentityHeader
from .checklist_view import ChecklistView
from .split_dialog import SplitDialog


_METADATA_DISPLAY_FIELDS: tuple[str, ...] = (
    "customer_name",
    "sales_order_number",
    "lead_time_days",
    "release_date",
)


class Dashboard(QWidget):
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)
    selection_changed = pyqtSignal(object)

    def __init__(
        self,
        checklist_service: ChecklistService,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        audit_metadata_service: AuditMetadataService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._checklist_service = checklist_service
        self._split_service = split_service
        self._completion_service = completion_service
        self._audit_metadata_service = audit_metadata_service
        self._view: ActiveAuditView | None = None
        self._current_audit_id: int | None = None
        self._current_selection: SelectionIntent | None = None
        
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc_shortcut.activated.connect(self._clear_selection)
        
        layout = QVBoxLayout(self)
        
        self.header = IdentityHeader()
        self.header.back_requested.connect(self.exit_requested.emit)
        self.header.ship_date_commit_requested.connect(self._on_ship_date_commit)
        layout.addWidget(self.header)
        
        self.metadata_band = QWidget()
        self.metadata_layout = QHBoxLayout(self.metadata_band)
        self.metadata_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.metadata_band)
        
        self.checklist = ChecklistView()
        self.checklist.toggle_requested.connect(self._on_row_toggle)
        self.checklist.notes_commit_requested.connect(self._on_row_notes_commit)
        self.checklist.body_clicked.connect(self._on_row_body_clicked)
        self.checklist.empty_space_clicked.connect(self._on_empty_space_clicked)
        layout.addWidget(self.checklist, stretch=1)
        
        footer = QHBoxLayout()
        self.split_btn = QPushButton("Split")
        self.split_btn.clicked.connect(self._on_split_clicked)
        footer.addWidget(self.split_btn)
        
        footer.addStretch()
        
        self.verify_all_btn = QPushButton("Verify All")
        self.verify_all_btn.clicked.connect(self._on_verify_all_clicked)
        footer.addWidget(self.verify_all_btn)
        
        self.complete_btn = QPushButton("Mark Complete")
        self.complete_btn.clicked.connect(self._on_complete_clicked)
        footer.addWidget(self.complete_btn)
        
        layout.addLayout(footer)

    def load(self, audit_id: int) -> None:
        self._clear_selection()
        self._current_audit_id = audit_id
        try:
            self._view = self._checklist_service.load_active_audit(audit_id)
            self._apply_view()
        except Exception as e:
            self.error_occurred.emit(render(e))

    def reload(self) -> None:
        self._clear_selection()
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
        for key in _METADATA_DISPLAY_FIELDS:
            val = metadata.get(key, "—")
            self.metadata_layout.addWidget(QLabel(f"{key}: {val}"))
            
        self.checklist.populate(self._view)
        self._update_enablement()

    def _update_enablement(self) -> None:
        if self._view is None:
            return
            
        if self._view.status == AuditStatus.COMPLETED:
            self.checklist.setEnabled(False)
            self.split_btn.setEnabled(False)
            self.verify_all_btn.setEnabled(False)
            self.complete_btn.setEnabled(False)
            self.header.ship_date_fld.setEnabled(False)
        else:
            self.checklist.setEnabled(True)
            self.split_btn.setEnabled(True)
            self.verify_all_btn.setEnabled(not self._view.is_fully_verified)
            self.complete_btn.setEnabled(self._view.is_fully_verified)
            self.header.ship_date_fld.setEnabled(True)

    def _current_notes_for(self, row_key: ChecklistRowKey) -> str | None:
        if self._view is None:
            raise KeyError()
        target = self._view.tht_rows if row_key.kind == "tht" else self._view.notes_rows
        for r in target:
            if r.key == row_key:
                return r.notes
        raise KeyError()

    def _on_row_toggle(self, row_key: ChecklistRowKey, new_state: bool) -> None:
        try:
            updated = self._checklist_service.set_verification(
                row_key, new_state, self._current_notes_for(row_key)
            )
            self._view = self._view.with_row_replaced(updated)
            self.checklist.update_row(updated)
            self._update_enablement()
        except PersistenceError as exc:
            self.checklist.revert_row(row_key)
            self.reload()
            self.error_occurred.emit(render(exc))

    def _on_row_notes_commit(self, row_key: ChecklistRowKey, new_notes: str | None) -> None:
        if self._view is None:
            return
            
        target = self._view.tht_rows if row_key.kind == "tht" else self._view.notes_rows
        is_verified = False
        for r in target:
            if r.key == row_key:
                is_verified = r.is_verified
                break
                
        try:
            updated = self._checklist_service.set_verification(
                row_key, is_verified, new_notes
            )
            self._view = self._view.with_row_replaced(updated)
            self.checklist.update_row(updated)
        except PersistenceError as exc:
            self.checklist.revert_row(row_key)
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
            self.error_occurred.emit(render(e))

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
            payload = render(exc)
            self.error_occurred.emit(payload)
            self.reload()
        except CleanupFailedError as exc:
            payload = render(exc)
            self.error_occurred.emit(payload)
            self.exit_requested.emit()
        except PersistenceError as exc:
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
            self.checklist.populate(reloaded)
            self._update_enablement()
        except PersistenceError as exc:
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
            self.header.ship_date_revert()
            self.reload()
            payload = render(exc)
            self.error_occurred.emit(payload)

    def _on_row_body_clicked(self, row_key: ChecklistRowKey) -> None:
        if self._view is None:
            return
        
        if row_key.kind == ChecklistRowKind.NOTES:
            return

        try:
            mpn = next(
                row.primary_label
                for row in self._view.tht_rows
                if row.key == row_key
            )
        except StopIteration:
            return

        if (self._current_selection is not None
                and self._current_selection.kind == SelectionKind.THT_MPN
                and self._current_selection.mpn == mpn):
            self._clear_selection()
            return

        intent = SelectionIntent(kind=SelectionKind.THT_MPN, mpn=mpn)
        self._current_selection = intent
        self.checklist.set_selected_row(row_key)
        self.selection_changed.emit(intent)

    def _on_empty_space_clicked(self) -> None:
        self._clear_selection()

    def _clear_selection(self) -> None:
        self._current_selection = None
        self.checklist.clear_selected_row()
        self.selection_changed.emit(SelectionIntent(kind=SelectionKind.CLEAR, mpn=None))
