"""Dashboard widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QSplitter
)

from cockpit.services.checklist import ChecklistService
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.services.split import AuditSplitService
from cockpit.services.views import ActiveAuditView, ChecklistRowKey, SelectionIntent, SelectionKind, ChecklistRowKind
from cockpit.ingestion.service import IngestionService
from cockpit.services.release import ReleaseService
from cockpit.services.setup_bom import SetupBomService
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




class Dashboard(QWidget):
    exit_requested = pyqtSignal()
    error_occurred = pyqtSignal(object)
    reload_requested = pyqtSignal(int)
    metadata_changed = pyqtSignal(dict)
    
    tht_body_clicked = pyqtSignal(object)
    tht_mpn_clicked = pyqtSignal(object)
    empty_clicked = pyqtSignal()
    esc_pressed = pyqtSignal()

    def __init__(
        self,
        checklist_service: ChecklistService,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        ingestion_service: IngestionService,
        release_service: ReleaseService,
        setup_bom_service: SetupBomService,
        theme: Theme,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._theme = theme
        self._checklist_service = checklist_service
        self._split_service = split_service
        self._completion_service = completion_service
        self._ingestion_service = ingestion_service
        self._release_service = release_service
        self._setup_bom_service = setup_bom_service
        self._view: ActiveAuditView | None = None
        self._current_audit_id: int | None = None
        
        self._esc_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        self._esc_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self._esc_shortcut.activated.connect(self.esc_pressed.emit)
        
        layout = QVBoxLayout(self)
        
        self.header = IdentityHeader()
        self.header.back_requested.connect(self._on_back_requested)
        layout.addWidget(self.header)
        
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
        from PyQt6.QtWidgets import QToolButton, QMenu
        self.actions_menu_btn = QToolButton()
        self.actions_menu_btn.setText("⋯")
        self.actions_menu_btn.setStyleSheet("QToolButton::menu-indicator { image: none; } QToolButton { font-weight: bold; font-size: 16px; padding: 2px 8px; }")
        self.actions_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.actions_menu = QMenu()
        self.actions_menu_btn.setMenu(self.actions_menu)
        self.actions_menu.aboutToShow.connect(self._rebuild_actions_menu)
        footer.addWidget(self.actions_menu_btn)
        
        footer.addStretch()
        
        self.verify_all_btn = QPushButton("Verify All")
        self.verify_all_btn.clicked.connect(self._on_verify_all_clicked)
        footer.addWidget(self.verify_all_btn)
        
        self.complete_btn = QPushButton("Complete")
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
        metadata = self._view.traveler_metadata or {}
        self.metadata_changed.emit(metadata)
        self.checklist_tht.populate_section(self._view.tht_rows, f"T/H - MPN Count: {len(self._view.tht_rows)} | Total Placements: {self._view.tht_placement_count}")
        self.checklist_notes.populate_section(self._view.notes_rows, f"Build Notes ({len(self._view.notes_rows)} items)")
        self._update_enablement()

    def _rebuild_actions_menu(self) -> None:
        if self._view is None:
            return
            
        self.actions_menu.clear()
        
        add_drawing_action = self.actions_menu.addAction("Replace" if self._view.has_pdf else "Add Drawing")
        add_drawing_action.triggered.connect(self._on_add_drawing_clicked)
        
        split_action = self.actions_menu.addAction("Split")
        split_action.triggered.connect(self._on_split_clicked)
        
        release_action = self.actions_menu.addAction("Release…")
        release_action.triggered.connect(self._on_release_clicked)
        
        setup_action = self.actions_menu.addAction("Setup…")
        setup_action.triggered.connect(self._on_setup_clicked)

    def flush_audit_notes(self) -> None:
        pass

    def _on_back_requested(self) -> None:
        self.metadata_changed.emit({})
        self.flush_audit_notes()
        self.exit_requested.emit()

    def apply_filter(self, query: str) -> None:
        self.checklist_tht.apply_filter(query)
        self.checklist_notes.apply_filter(query)

    def _update_enablement(self) -> None:
        if self._view is None:
            return
            
        self.checklist_tht.setEnabled(True)
        self.checklist_notes.setEnabled(True)
        
        is_verified = self._view.is_fully_verified
        
        # Check if we are transitioning from unverified to verified
        just_verified = is_verified and not self.complete_btn.isVisible()
        
        self.verify_all_btn.setVisible(not is_verified)
        self.verify_all_btn.setEnabled(not is_verified)
        
        self.complete_btn.setVisible(is_verified)
        
        if just_verified:
            self.complete_btn.setEnabled(False)
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(500, lambda: self.complete_btn.setEnabled(True))
        else:
            self.complete_btn.setEnabled(is_verified)



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
        from cockpit.ui.widgets.add_drawing_dialog import AddDrawingDialog
        dialog = AddDrawingDialog(self._ingestion_service, self._view.audit_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_requested.emit(self._view.audit_id)

    def _on_release_clicked(self) -> None:
        if self._view is None:
            return
            
        try:
            defaults = self._release_service.build_defaults(self._view)
            
            from cockpit.ui.widgets.release_dialog import ReleaseDialog
            from PyQt6.QtWidgets import QDialog
            
            dialog = ReleaseDialog(defaults, self._view.status, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                new_data, new_status = dialog.get_result()
                
                # 1. Persist the workflow_status before print (per spec)
                from cockpit.persistence.repositories.audits import AuditRepository
                self._release_service.transition_status(self._view.audit_id, new_status)
                self.reload() # update view with new status
                
                # 2. Print
                from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
                printer = QPrinter(QPrinter.PrinterMode.HighResolution)
                print_dialog = QPrintDialog(printer, self)
                if print_dialog.exec():
                    self._release_service.print_release_form(new_data, printer)
        except Exception as e:
            logger.exception('Exception caught in dashboard')
            self.error_occurred.emit(render(e))
        
    def _on_setup_clicked(self) -> None:
        if self._view is None:
            return
            
        try:
            from cockpit.ui.widgets.setup_dialog import SetupDialog
            from PyQt6.QtWidgets import QDialog, QMessageBox
            
            dialog = SetupDialog(self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                side, process = dialog.get_filters()
                rows = self._setup_bom_service.build(self._view.audit_id, side, process)
                if not rows:
                    QMessageBox.warning(self, "No Components", "No components found for the selected Side and Process filters. Nothing to print.")
                    return
                
                from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
                printer = QPrinter(QPrinter.PrinterMode.HighResolution)
                print_dialog = QPrintDialog(printer, self)
                if print_dialog.exec():
                    self._setup_bom_service.print_bom(rows, printer)
        except Exception as e:
            logger.exception('Exception caught in dashboard setup')
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
            self.checklist_tht.populate_section(reloaded.tht_rows, f"T/H Verification ({len(reloaded.tht_rows)} items)")
            self.checklist_notes.populate_section(reloaded.notes_rows, f"Build Notes ({len(reloaded.notes_rows)} items)")
            self._update_enablement()
        except PersistenceError as exc:
            logger.exception('Exception caught in dashboard')
            self.reload()
            payload = render(exc)
            self.error_occurred.emit(payload)
