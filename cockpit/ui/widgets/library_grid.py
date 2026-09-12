import logging
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QLineEdit, QComboBox, QHBoxLayout, QLabel, QMenu, QInputDialog, QMessageBox
)
from PyQt6.QtCore import Qt, pyqtSignal
from cockpit.services.mpn_library.types import (
    MpnKey, AttributeName, Provenance, PartAttributeDraft, ResolvedAttribute, Absent, ObservationSummary
)
from cockpit.services.mpn_library.registry import ATTRIBUTE_REGISTRY

logger = logging.getLogger(__name__)

class LibraryGridWidget(QWidget):
    def __init__(self, repository, utcnow, parent=None):
        super().__init__(parent)
        self.repository = repository
        self.utcnow = utcnow
        self._parts = [] # List of tuples: (mpn_key, mpn_as_seen, resolution_status, is_unkeyable)
        self._operation_in_flight = False
        
        layout = QVBoxLayout(self)
        
        # Filter row
        filter_layout = QHBoxLayout()
        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Filter MPN or attributes...")
        self.filter_input.textChanged.connect(self._apply_filter)
        filter_layout.addWidget(QLabel("Filter:"))
        filter_layout.addWidget(self.filter_input)
        layout.addLayout(filter_layout)
        
        # Table
        self.table = QTableWidget()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self.table)
        
        # Set up columns
        self.attr_names = list(ATTRIBUTE_REGISTRY.keys())
        headers = ["MPN", "Status"] + [ATTRIBUTE_REGISTRY[an].display for an in self.attr_names]
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        
    def set_operation_in_flight(self, in_flight: bool):
        self._operation_in_flight = in_flight
        
    def populate(self, observation_summary: ObservationSummary):
        # We need to fetch all parts for the current audit, but we only got observation summary.
        # Actually, observe_bom inserted them into the DB. We need to query them.
        # Let's add a query in repository to get all parts, or we can just pass the list of parts to this method.
        # The design says "One row per distinct normalised MPN on the current audit BOM."
        # We will fetch them in the segment and pass them here.
        pass
        
    def set_parts(self, parts):
        self._parts = parts
        self._render()
        
    def _render(self):
        self.table.setRowCount(0)
        filter_text = self.filter_input.text().lower()
        
        row_idx = 0
        for mpn_key, mpn_as_seen, status, is_unkeyable in self._parts:
            # Check filter
            match = filter_text in mpn_as_seen.lower()
            
            # Fetch attributes to see if they match filter and to render
            resolved_attrs = {}
            if not is_unkeyable:
                for an in self.attr_names:
                    res = self.repository.resolve_attribute(mpn_key, an)
                    resolved_attrs[an] = res
                    if isinstance(res, ResolvedAttribute) and res.value_text and filter_text in res.value_text.lower():
                        match = True
            
            if filter_text and not match:
                continue
                
            self.table.insertRow(row_idx)
            
            item_mpn = QTableWidgetItem(mpn_as_seen)
            item_mpn.setData(Qt.ItemDataRole.UserRole, (mpn_key, is_unkeyable))
            self.table.setItem(row_idx, 0, item_mpn)
            
            item_status = QTableWidgetItem(status)
            self.table.setItem(row_idx, 1, item_status)
            
            if is_unkeyable:
                for c_idx in range(len(self.attr_names)):
                    self.table.setItem(row_idx, 2 + c_idx, QTableWidgetItem("N/A"))
            else:
                for c_idx, an in enumerate(self.attr_names):
                    res = resolved_attrs[an]
                    if isinstance(res, ResolvedAttribute):
                        text = res.value_text
                        if res.provenance == Provenance.OPERATOR_OVERRIDE:
                            text = f"[O] {text}"
                        elif res.provenance == Provenance.DIGIKEY:
                            text = f"[D] {text}"
                        item = QTableWidgetItem(text)
                        if res.confirmed_at:
                            item.setToolTip(f"Confirmed: {res.confirmed_at}")
                        self.table.setItem(row_idx, 2 + c_idx, item)
                    else:
                        self.table.setItem(row_idx, 2 + c_idx, QTableWidgetItem(""))
            row_idx += 1

    def _apply_filter(self, text):
        self._render()

    def _on_context_menu(self, pos):
        if self._operation_in_flight:
            return
            
        item = self.table.itemAt(pos)
        if not item:
            return
            
        row = item.row()
        col = item.column()
        
        mpn_item = self.table.item(row, 0)
        mpn_key, is_unkeyable = mpn_item.data(Qt.ItemDataRole.UserRole)
        if is_unkeyable:
            return
            
        status_item = self.table.item(row, 1)
        status = status_item.text()
        
        menu = QMenu(self)
        
        choose_variant_action = None
        if status == "AMBIGUOUS":
            choose_variant_action = menu.addAction("Choose Variant...")
            
        # We also have edit/revoke override for columns > 1
        edit_action = None
        revoke_action = None
        attr_name = None
        res = None
        
        if col >= 2:
            attr_name = self.attr_names[col - 2]
            edit_action = menu.addAction("Edit / Override...")
            revoke_action = menu.addAction("Revoke Override")
            
            res = self.repository.resolve_attribute(mpn_key, attr_name)
            has_override = isinstance(res, ResolvedAttribute) and res.provenance == Provenance.OPERATOR_OVERRIDE
            revoke_action.setEnabled(has_override)
        
        action = menu.exec(self.table.viewport().mapToGlobal(pos))
        if not action:
            return
            
        if choose_variant_action and action == choose_variant_action:
            self._do_choose_variant(mpn_key)
        elif edit_action and action == edit_action:
            current_val = res.value_text if isinstance(res, ResolvedAttribute) else ""
            new_val, ok = QInputDialog.getText(self, "Edit Attribute", f"Enter value for {ATTRIBUTE_REGISTRY[attr_name].display}:", text=current_val)
            if ok and new_val:
                val_numeric = None
                spec = ATTRIBUTE_REGISTRY[attr_name]
                if spec.value_kind.name == "NUMERIC":
                    try:
                        import re
                        m = re.search(r"[-+]?\d*\.\d+|\d+", new_val)
                        if m:
                            val_numeric = float(m.group())
                    except ValueError:
                        pass
                
                draft = PartAttributeDraft(
                    mpn_key=mpn_key,
                    attribute_name=attr_name,
                    provenance=Provenance.OPERATOR_OVERRIDE,
                    value_text=new_val,
                    value_numeric=val_numeric,
                    source_label=None
                )
                self.repository.record_override(draft, self.utcnow)
                # Since we don't have a way to emit back to segment easily here, we just re-render.
                # Actually we should update the part's status if it becomes usable, but that's driven by observation.
                self._render()
                
        elif revoke_action and action == revoke_action:
            self.repository.revoke_override(mpn_key, attr_name)
            self._render()

    def _do_choose_variant(self, mpn_key: str):
        # Fetch snapshot
        cur = self.repository._conn.cursor()
        cur.execute(
            "SELECT payload_json FROM digikey_snapshot WHERE mpn_key = ? ORDER BY fetched_at DESC LIMIT 1",
            (mpn_key,)
        )
        row = cur.fetchone()
        if not row:
            QMessageBox.warning(self, "No Snapshot", "No snapshot found. Please Re-query.")
            return
            
        from cockpit.services.mpn_library.digikey_variant import restore_candidates_from_snapshot
        from cockpit.services.mpn_library.digikey_types import SnapshotUnusable
        
        candidates = restore_candidates_from_snapshot(mpn_key, row["payload_json"])
        if isinstance(candidates, SnapshotUnusable):
            QMessageBox.warning(self, "Snapshot Unusable", "Snapshot is no longer usable. Please Re-query.")
            return
            
        items = []
        for i, c in enumerate(candidates):
            lbl = c.packaging_label if c.packaging_label else str(c.packaging_type.value)
            items.append(f"{c.digikey_part_number} ({c.manufacturer}) - {lbl}")
            
        choice, ok = QInputDialog.getItem(self, "Choose Variant", "Select the correct variant:", items, 0, False)
        if ok and choice:
            idx = items.index(choice)
            selected = candidates[idx]
            
            # Commit attributes
            now_iso = self.utcnow().isoformat()
            cur.execute("BEGIN IMMEDIATE")
            try:
                for attr in selected.attributes:
                    cur.execute(
                        """
                        SELECT value_text, value_numeric, confirmed_at 
                        FROM part_attribute 
                        WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                        """,
                        (mpn_key, attr.attribute_name, Provenance.DIGIKEY.value)
                    )
                    r = cur.fetchone()
                    if r:
                        if r["value_text"] != attr.value_text or r["value_numeric"] != attr.value_numeric:
                            confirmed_at = None
                        else:
                            confirmed_at = r["confirmed_at"]
                            
                        cur.execute(
                            """
                            UPDATE part_attribute 
                            SET value_text = ?, value_numeric = ?, confirmed_at = ? 
                            WHERE mpn_key = ? AND attribute_name = ? AND provenance = ?
                            """,
                            (attr.value_text, attr.value_numeric, confirmed_at, mpn_key, attr.attribute_name, Provenance.DIGIKEY.value)
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO part_attribute 
                            (mpn_key, attribute_name, provenance, value_text, value_numeric, source_label, recorded_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (mpn_key, attr.attribute_name, Provenance.DIGIKEY.value, attr.value_text, attr.value_numeric, attr.source_label, now_iso)
                        )
                
                cur.execute(
                    "UPDATE library_part SET resolution_status = 'RESOLVED' WHERE mpn_key = ?",
                    (mpn_key,)
                )
                self.repository._conn.commit()
            except Exception:
                self.repository._conn.rollback()
                QMessageBox.critical(self, "Error", "Failed to save variant.")
                return
                
            # Update the cached status
            for i, p in enumerate(self._parts):
                if p[0] == mpn_key:
                    self._parts[i] = (p[0], p[1], 'RESOLVED', p[3])
                    break
            self._render()

    def update_part(self, mpn_key: str, status: str):
        for i, p in enumerate(self._parts):
            if p[0] == mpn_key:
                self._parts[i] = (p[0], p[1], status, p[3])
                break
        self._render()
