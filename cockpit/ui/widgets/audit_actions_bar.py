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
    # Enumeration blocks on a network share, so it runs on MainWindow's
    # existing fetch worker and watchdog. This widget holds no threading code.
    drawing_fetch_requested = pyqtSignal(int, bool)  # (audit_id, secondary)
    # An audit's own scalar fields changed and nothing else did. No source
    # file, BOM row or PDF was touched, so no consumer may rebuild the BOM
    # panel or request a canvas render.
    audit_fields_changed = pyqtSignal(int)

    def __init__(
        self,
        split_service: AuditSplitService,
        completion_service: CompletionService,
        ingestion_service: IngestionService,
        release_service: ReleaseService,
        setup_bom_service: SetupBomService,
        parent: QWidget | None = None,
        source_root_controller=None,
    ) -> None:
        super().__init__(parent)
        self._split_service = split_service
        self._completion_service = completion_service
        self._ingestion_service = ingestion_service
        self._release_service = release_service
        self._setup_bom_service = setup_bom_service
        # Only to decide Fetch's enabled state and tooltip. No path is built
        # from it here.
        self._source_root_controller = source_root_controller
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

        # Built once and repopulated on rebuild. QMenu.clear() removes a
        # submenu's action without destroying the submenu, so recreating them
        # on every aboutToShow would accumulate them for the view's lifetime.
        self._add_menu = QMenu("Add", self.actions_menu)
        self._update_menu = QMenu("Update", self.actions_menu)
        self._print_menu = QMenu("Print", self.actions_menu)
        self._role_menus: dict[tuple[str, bool], QMenu] = {}
        for parent_menu, key in ((self._add_menu, "add"), (self._update_menu, "update")):
            for secondary in (False, True):
                label = "Secondary Drawing" if secondary else "Drawing"
                self._role_menus[(key, secondary)] = QMenu(label, parent_menu)
        layout.addWidget(self.actions_menu_btn)
        
        self.complete_btn = QPushButton("Complete")
        self.complete_btn.clicked.connect(self._on_complete_clicked)
        layout.addWidget(self.complete_btn)

    def bind(self, session: AuditSession) -> None:
        self._session = session
        
    def unload(self) -> None:
        self.actions_menu.clear()
        for menu in (self._add_menu, self._update_menu, self._print_menu):
            menu.clear()
        for menu in self._role_menus.values():
            menu.clear()

    def _fetch_unavailable_reason(self, view) -> str | None:
        """Why Fetch cannot run for this audit, or None when it can.

        post: share reachability is never probed here; stat-ing a network path
              while building a menu would reintroduce the freeze the fetch
              watchdog exists to prevent
        """
        from cockpit.ingestion.locator import JOB_NUMBER_GRAMMAR
        from cockpit.settings.source_root import SourceRootState

        if self._source_root_controller is None:
            return "Source root is not configured. Settings \u203a Source Root."

        state, _ = self._source_root_controller.source_root()
        if state == SourceRootState.UNSET:
            return "Source root is not configured. Settings \u203a Source Root."
        if state != SourceRootState.CONFIGURED:
            return "Source root is not a valid path. Settings \u203a Source Root."

        if not JOB_NUMBER_GRAMMAR.match(str(view.part_number or "")):
            return f"{view.part_number} is not a job number; use Manual."
        return None

    def _populate_role_menu(self, menu: QMenu, secondary: bool, view) -> None:
        menu.clear()
        fetch_action = menu.addAction("Fetch")
        reason = self._fetch_unavailable_reason(view)
        if reason is None:
            fetch_action.triggered.connect(
                lambda _checked=False, sec=secondary: self.drawing_fetch_requested.emit(
                    self._session.current_view().audit_id, sec
                )
            )
        else:
            fetch_action.setEnabled(False)
            fetch_action.setToolTip(reason)

        manual_action = menu.addAction("Manual")
        if secondary:
            manual_action.triggered.connect(self._on_add_secondary_drawing_clicked)
        else:
            manual_action.triggered.connect(self._on_add_drawing_clicked)

    def _rebuild_actions_menu(self) -> None:
        if self._session is None:
            return
            
        view = self._session.current_view()
        if not view:
            return
            
        self.actions_menu.clear()
        self._add_menu.setToolTipsVisible(True)
        self._update_menu.setToolTipsVisible(True)

        # A role appears under exactly one of Add and Update, never both.
        present = {False: view.has_pdf, True: view.has_secondary_pdf}

        addable = [sec for sec in (False, True) if not present[sec]]
        if addable:
            self._add_menu.clear()
            for sec in addable:
                role_menu = self._role_menus[("add", sec)]
                self._populate_role_menu(role_menu, sec, view)
                self._add_menu.addMenu(role_menu)
            self.actions_menu.addMenu(self._add_menu)

        updatable = [sec for sec in (False, True) if present[sec]]
        if updatable:
            self._update_menu.clear()
            for sec in updatable:
                role_menu = self._role_menus[("update", sec)]
                self._populate_role_menu(role_menu, sec, view)
                self._update_menu.addMenu(role_menu)
            self.actions_menu.addMenu(self._update_menu)

        self._print_menu.clear()
        release_action = self._print_menu.addAction("Release form…")
        release_action.triggered.connect(self._on_release_clicked)
        setup_action = self._print_menu.addAction("Setup sheet…")
        setup_action.triggered.connect(self._on_setup_clicked)
        self.actions_menu.addMenu(self._print_menu)

        split_action = self.actions_menu.addAction("Split")
        split_action.triggered.connect(self._on_split_clicked)

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
            
            from cockpit.ui.widgets.release_dialog import (
                DetailsOutcome, ReleaseDialog,
            )

            dialog = ReleaseDialog(defaults, view.status, self)
            dialog.update_requested.connect(
                lambda d=dialog, aid=view.audit_id: self._commit_details(d, aid)
            )

            accepted = dialog.exec() == QDialog.DialogCode.Accepted
            if not accepted or dialog.outcome() != DetailsOutcome.RELEASE:
                return

            # Release commits on the way out, then prints. Update may already
            # have committed in place; re-committing identical values is a
            # harmless no-op that keeps this path from having to know.
            if not self._commit_details(dialog, view.audit_id, quiet=True):
                return

            new_data, _status = dialog.get_result()
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            print_dialog = QPrintDialog(printer, self)
            if print_dialog.exec():
                self._release_service.print_release_form(new_data, printer)
        except Exception as e:
            logger.exception('Exception caught in AuditActionsBar release')
            self.error_occurred.emit(render(e))

    def _commit_details(self, dialog, audit_id: int, quiet: bool = False) -> bool:
        """Persists the modal's two persistable fields.

        pre:  dialog holds the form the operator is looking at
        post: on success status and ship_date are committed, the dialog is
              rebased onto them, and audit_fields_changed is emitted; on
              failure nothing is committed and the dialog reports inline
        """
        from cockpit.ui.widgets.release_dialog import resolve_ship_date

        new_data, new_status = dialog.get_result()
        try:
            # Blank clears; anything else that will not parse raises rather
            # than falling through as a NULL that clears a ship date the
            # operator never asked to clear.
            ship_date_obj = resolve_ship_date(new_data.ship_date, dialog.ship_date_is_blank())
            self._release_service.persist_release(audit_id, AuditStatus(new_status), ship_date_obj)
        except Exception as e:
            logger.exception('Exception caught committing Details')
            if quiet:
                self.error_occurred.emit(render(e))
            else:
                # Inline rather than a second dialog over the open modal.
                dialog.mark_commit_failed(str(e))
            return False

        if not quiet:
            dialog.mark_committed(new_status, new_data.ship_date)
        # Not reload_requested: nothing this commit touched can change a BOM
        # row or a drawing, so nothing may rebuild the panel or the canvas.
        self.audit_fields_changed.emit(audit_id)
        return True
        
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
