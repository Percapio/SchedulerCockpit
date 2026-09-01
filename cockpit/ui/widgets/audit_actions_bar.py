import logging
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QPushButton, QToolButton, QMenu, QDialog, QMessageBox
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtPrintSupport import QPrintDialog, QPrinter
from datetime import date

from cockpit.services.split import AuditSplitService
from cockpit.services.completion import CompletionService, CleanupFailedError
from cockpit.ingestion.service import IngestionService
from cockpit.services.release import ReleaseService
from cockpit.services.setup_bom import SetupBomService
from cockpit.persistence.types import AuditStatus
from cockpit.persistence.errors import PersistenceError, IllegalStateTransition
from cockpit.ui.error_messages import render

from .split_dialog import SplitDialog
from .audit_session import AuditSession

logger = logging.getLogger(__name__)

class AuditActionsBar(QWidget):
    error_occurred = pyqtSignal(object)
    reload_requested = pyqtSignal(int)
    ops_per_board_change_requested = pyqtSignal(int, object)
    exit_requested = pyqtSignal()
    second_ops_requested = pyqtSignal(int)

    def __init__(
        self,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        ingestion_service: IngestionService,
        release_service: ReleaseService,
        setup_bom_service: SetupBomService,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._split_service = split_service
        self._completion_service = completion_service
        self._ingestion_service = ingestion_service
        self._release_service = release_service
        self._setup_bom_service = setup_bom_service
        self._session: AuditSession | None = None
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.actions_menu_btn = QToolButton()
        self.actions_menu_btn.setText("⋯")
        self.actions_menu_btn.setStyleSheet("QToolButton::menu-indicator { image: none; } QToolButton { font-weight: bold; font-size: 16px; padding: 2px 8px; }")
        self.actions_menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.actions_menu = QMenu()
        self.actions_menu_btn.setMenu(self.actions_menu)
        self.actions_menu.aboutToShow.connect(self._rebuild_actions_menu)
        layout.addWidget(self.actions_menu_btn)
        
        self.complete_btn = QPushButton("Complete")
        self.complete_btn.clicked.connect(self._on_complete_clicked)
        layout.addWidget(self.complete_btn)

    def bind(self, session: AuditSession) -> None:
        self._session = session
        
    def unload(self) -> None:
        self.actions_menu.clear()

    def _rebuild_actions_menu(self) -> None:
        if self._session is None:
            return
            
        view = self._session.current_view()
        if not view:
            return
            
        self.actions_menu.clear()
        
        add_drawing_action = self.actions_menu.addAction("Replace" if view.has_pdf else "Add Drawing")
        add_drawing_action.triggered.connect(self._on_add_drawing_clicked)
        
        add_sec_action = self.actions_menu.addAction("Replace Secondary Drawing" if view.has_secondary_pdf else "Add Secondary Drawing")
        add_sec_action.triggered.connect(self._on_add_secondary_drawing_clicked)
        
        split_action = self.actions_menu.addAction("Split")
        split_action.triggered.connect(self._on_split_clicked)
        
        release_action = self.actions_menu.addAction("Release…")
        release_action.triggered.connect(self._on_release_clicked)
        
        setup_action = self.actions_menu.addAction("Setup…")
        setup_action.triggered.connect(self._on_setup_clicked)
        
        second_ops_action = self.actions_menu.addAction("2nd OPS…")
        second_ops_action.triggered.connect(self._on_second_ops_clicked)
        
        ops_action = self.actions_menu.addAction("OPS per board…")
        ops_action.triggered.connect(self._on_ops_per_board_clicked)

    def _on_split_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
            
        dialog = SplitDialog(view, self._split_service, self)
        try:
            if dialog.exec():
                if dialog.outcome:
                    self.reload_requested.emit(view.audit_id)
                    win = self.window()
                    if hasattr(win, "toast"):
                        win.toast.show_toast(f"Split into {dialog.outcome.sibling_suffix} (qty {dialog.outcome.sibling_quantity})", "")
        except Exception as e:
            logger.exception('Exception caught in AuditActionsBar split')
            self.error_occurred.emit(render(e))

    def _on_add_drawing_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
        from cockpit.ui.widgets.add_drawing_dialog import AddDrawingDialog
        dialog = AddDrawingDialog(self._ingestion_service, view.audit_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_requested.emit(view.audit_id)

    def _on_add_secondary_drawing_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
        from cockpit.ui.widgets.add_drawing_dialog import AddDrawingDialog
        dialog = AddDrawingDialog(self._ingestion_service, view.audit_id, self, secondary=True)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_requested.emit(view.audit_id)

    def _on_release_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
            
        try:
            defaults = self._release_service.build_defaults(view)
            
            from cockpit.ui.widgets.release_dialog import ReleaseDialog
            
            dialog = ReleaseDialog(defaults, view.status, self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                new_data, new_status = dialog.get_result()
                
                ship_date_obj = None
                if new_data.ship_date:
                    try:
                        ship_date_obj = date.fromisoformat(new_data.ship_date)
                    except ValueError:
                        pass
                new_status_enum = AuditStatus(new_status)
                self._release_service.persist_release(view.audit_id, new_status_enum, ship_date_obj)
                self.reload_requested.emit(view.audit_id)
                
                printer = QPrinter(QPrinter.PrinterMode.HighResolution)
                print_dialog = QPrintDialog(printer, self)
                if print_dialog.exec():
                    self._release_service.print_release_form(new_data, printer)
        except Exception as e:
            logger.exception('Exception caught in AuditActionsBar release')
            self.error_occurred.emit(render(e))
        
    def _on_setup_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
            
        try:
            from cockpit.ui.widgets.setup_dialog import SetupDialog
            
            dialog = SetupDialog(self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                side, process = dialog.get_filters()
                rows = self._setup_bom_service.build(view.audit_id, side, process)
                if not rows:
                    QMessageBox.warning(self, "No Components", "No components found for the selected Side and Process filters. Nothing to print.")
                    return
                
                printer = QPrinter(QPrinter.PrinterMode.HighResolution)
                print_dialog = QPrintDialog(printer, self)
                if print_dialog.exec():
                    self._setup_bom_service.print_bom(rows, printer)
        except Exception as e:
            logger.exception('Exception caught in AuditActionsBar setup')
            self.error_occurred.emit(render(e))

    def _on_ops_per_board_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
        from cockpit.ui.widgets.dialogs import OpsPerBoardDialog
        dialog = OpsPerBoardDialog(view.ops_per_board_min, self)
        if dialog.exec():
            self.ops_per_board_change_requested.emit(view.audit_id, dialog.result_value())

    def _on_second_ops_clicked(self) -> None:
        if not self._session:
            return
        view = self._session.current_view()
        if not view:
            return
        self.second_ops_requested.emit(view.audit_id)

    def _on_complete_clicked(self) -> None:
        if not self._session:
            return
        audit_id = self._session.current_audit_id()
        if audit_id is None:
            return
            
        from cockpit.ui.widgets.dialogs import confirm_destructive
        if not confirm_destructive("Complete Audit", "Are you sure you want to complete this audit? This action cannot be undone and will delete the audit files.", "Complete", self):
            return
            
        try:
            outcome = self._completion_service.complete_and_cleanup(audit_id)
            win = self.window()
            if hasattr(win, "toast"):
                win.toast.show_toast(f"Completed and cleaned up", "")
            self.exit_requested.emit()
        except IllegalStateTransition as exc:
            logger.exception('Exception caught in AuditActionsBar complete')
            self.error_occurred.emit(render(exc))
            self.reload_requested.emit(audit_id)
        except CleanupFailedError as exc:
            logger.exception('Exception caught in AuditActionsBar complete')
            self.error_occurred.emit(render(exc))
            self.exit_requested.emit()
        except PersistenceError as exc:
            logger.exception('Exception caught in AuditActionsBar complete')
            self.error_occurred.emit(render(exc))
            self.reload_requested.emit(audit_id)
