from typing import Callable, Optional, Any
from PyQt6.QtCore import QObject, pyqtSignal
from cockpit.services.checklist import ChecklistService
from cockpit.services.views import ActiveAuditView, ChecklistRowKey, ChecklistRowView, AuditIdentityBanner
from cockpit.persistence.errors import PersistenceError
from cockpit.ui.error_messages import render
import logging

logger = logging.getLogger(__name__)

class AuditSession(QObject):
    view_changed = pyqtSignal(object) # ActiveAuditView
    identity_changed = pyqtSignal(object) # AuditIdentityBanner
    rows_replaced = pyqtSignal(object) # ActiveAuditView
    error_occurred = pyqtSignal(object) # FailurePayload
    reload_requested = pyqtSignal(int)
    ops_per_board_change_requested = pyqtSignal(int, object)

    def __init__(self, checklist_service: ChecklistService, build_banner: Callable[[ActiveAuditView], AuditIdentityBanner]):
        super().__init__()
        self._checklist_service = checklist_service
        self._build_banner = build_banner
        self._view: ActiveAuditView | None = None
        self._current_audit_id: int | None = None

    def load(self, audit_id: int) -> None:
        self.unload()
        self._current_audit_id = audit_id
        try:
            view = self._checklist_service.load_active_audit(audit_id)
            self._apply_view(view)
            self._view = view
        except Exception as e:
            logger.exception('Exception caught in AuditSession')
            self.error_occurred.emit(render(e))

    def reload(self) -> None:
        if self._current_audit_id is not None:
            try:
                view = self._checklist_service.load_active_audit(self._current_audit_id)
                self._apply_view(view)
                self._view = view
            except Exception as e:
                logger.exception('Exception caught in AuditSession reload')
                self.error_occurred.emit(render(e))

    def unload(self) -> None:
        self._view = None
        self._current_audit_id = None
        self._checklist_service.release_audit_scoped_caches()

    def current_view(self) -> Optional[ActiveAuditView]:
        return self._view

    def current_audit_id(self) -> Optional[int]:
        return self._current_audit_id

    def has_pdf(self) -> bool:
        return self._view is not None and self._view.has_pdf


            
    def _apply_view(self, view: ActiveAuditView) -> None:
        self.view_changed.emit(view)
        banner = self._build_banner(view)
        self.identity_changed.emit(banner)
        self.rows_replaced.emit(view)
