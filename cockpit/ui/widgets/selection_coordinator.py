"""Selection Coordinator for layout highlighting."""

from typing import Callable, Any
from PyQt6.QtCore import pyqtSignal, QObject
import logging

from cockpit.services.views import ActiveAuditView, ChecklistRowKey, ChecklistRowKind, SelectionIntent, SelectionKind

from cockpit.services.layout_query import LayoutQueryService

# Forward declarations for type hints without circular dependencies
class Dashboard(Any): ...
class AuditBomPanel(Any): ...

logger = logging.getLogger(__name__)

class SelectionCoordinator(QObject):
    selection_changed = pyqtSignal(object)  # Emits SelectionIntent

    def __init__(
        self,
        view_provider: Callable[[], ActiveAuditView | None],
        layout_query_service: LayoutQueryService
    ) -> None:
        super().__init__()
        self._view_provider = view_provider
        self._layout_query_service = layout_query_service
        self._dashboard = None
        self._bom_panel = None
        self._active: SelectionIntent | None = None
        self._selected_mpn_set: frozenset[str] = frozenset()
        self._selected_ref_des: str | None = None

    def on_renderer_refdes_clicked(self, ref_des: str) -> None:
        view = self._view_provider()
        if not view:
            return
            
        location = self._layout_query_service.locate_refdes(view.audit_id, ref_des)
        if not location:
            return
            
        if location.mount_type == 'T':
            row = next((r for r in view.tht_rows if ref_des in r.ref_des_list), None)
            if row:
                self.on_tht_refdes_clicked(ref_des)
                if self._dashboard:
                    self._dashboard.checklist_tht.scroll_to_row(row.key)
        elif location.mount_type == 'S':
            self.on_bom_refdes_selected(ref_des)
            if self._bom_panel:
                self._bom_panel.scroll_to_refdes(ref_des)

    def register_dashboard(self, dashboard: 'Dashboard') -> None:
        self._dashboard = dashboard

    def register_bom_panel(self, bom_panel: 'AuditBomPanel') -> None:
        self._bom_panel = bom_panel

    def on_tht_body_clicked(self, row_key: ChecklistRowKey) -> None:
        if row_key.kind != ChecklistRowKind.THT:
            logger.debug(f"Dropped body click for non-THT row {row_key}")
            return
            
        view = self._view_provider()
        if not view:
            logger.debug("Dropped body click: no active view")
            return
            
        row = next((r for r in view.tht_rows if r.key == row_key), None)
        if not row:
            logger.debug(f"Dropped body click: row {row_key} not in view")
            return

        mpn = row.primary_label
        self._clear_panes()
        self._selected_mpn_set = frozenset()
        self._selected_ref_des = None
        
        if self._dashboard:
            self._dashboard.checklist_tht.set_selected_row(row_key)
            
        self._emit(SelectionIntent(kind=SelectionKind.THT_MPN, mpn=mpn))

    def on_tht_mpn_clicked(self, row_key: ChecklistRowKey) -> None:
        if row_key.kind != ChecklistRowKind.THT:
            logger.debug(f"Dropped MPN click for non-THT row {row_key}")
            return
            
        view = self._view_provider()
        if not view:
            logger.debug("Dropped MPN click: no active view")
            return
            
        row = next((r for r in view.tht_rows if r.key == row_key), None)
        if not row:
            logger.debug(f"Dropped MPN click: row {row_key} not in view")
            return

        mpn = row.primary_label
        self._clear_panes()
        self._selected_mpn_set = frozenset()
        self._selected_ref_des = None
        
        if self._dashboard:
            self._dashboard.checklist_tht.set_selected_row(row_key)
            
        self._emit(SelectionIntent(kind=SelectionKind.MPN_CELL, mpn=mpn))

    def on_bom_mpn_toggled(self, mpn: str) -> None:
        if not mpn:
            return
            
        new_set = set(self._selected_mpn_set)
        if mpn in new_set:
            new_set.remove(mpn)
        else:
            new_set.add(mpn)
            
        self._selected_ref_des = None
        
        # We need to tell the dashboard to clear its selection
        if self._dashboard:
            self._dashboard.checklist_tht.clear_selected_row()
            if hasattr(self._dashboard, 'checklist_notes'):
                self._dashboard.checklist_notes.clear_selected_row()
                
        if not new_set:
            self._emit_clear()
        else:
            self._selected_mpn_set = frozenset(new_set)
            self._emit(SelectionIntent(kind=SelectionKind.BOM_MPN_SET, mpn_set=self._selected_mpn_set))

    def on_bom_refdes_selected(self, ref_des: str) -> None:
        if not ref_des:
            return
            
        if self._selected_ref_des == ref_des:
            self._emit_clear()
        else:
            self._selected_mpn_set = frozenset()
            self._selected_ref_des = ref_des
            
            if self._dashboard:
                self._dashboard.checklist_tht.clear_selected_row()
                if hasattr(self._dashboard, 'checklist_notes'):
                    self._dashboard.checklist_notes.clear_selected_row()
                    
            self._emit(SelectionIntent(kind=SelectionKind.BOM_REFDES, ref_des=ref_des))

    def on_tht_refdes_clicked(self, ref_des: str) -> None:
        self.on_bom_refdes_selected(ref_des)

    def on_empty_clicked(self) -> None:
        self._emit_clear()

    def on_escape_pressed(self) -> None:
        self._emit_clear()

    def on_audit_loaded(self) -> None:
        self._emit_clear()

    def _emit_clear(self) -> None:
        self._selected_mpn_set = frozenset()
        self._selected_ref_des = None
        self._clear_panes()
        
        if self._active is not None and self._active.kind != SelectionKind.CLEAR:
            self._active = SelectionIntent(kind=SelectionKind.CLEAR)
            self.selection_changed.emit(self._active)
        elif self._active is None:
            self._active = SelectionIntent(kind=SelectionKind.CLEAR)
            self.selection_changed.emit(self._active)

    def _emit(self, intent: SelectionIntent) -> None:
        if self._active == intent:
            return
        self._active = intent
        self.selection_changed.emit(intent)

    def _clear_panes(self) -> None:
        if self._dashboard:
            self._dashboard.checklist_tht.clear_selected_row()
            if hasattr(self._dashboard, 'checklist_notes'):
                self._dashboard.checklist_notes.clear_selected_row()
        if self._bom_panel:
            self._bom_panel.clear()
