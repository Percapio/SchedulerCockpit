from typing import Any, Optional
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QHBoxLayout, QPushButton, QMessageBox
from PyQt6.QtCore import Qt
from cockpit.services.mpn_library.module import MpnLibraryModule
from cockpit.persistence.clock import utcnow
from .library_grid import LibraryGridWidget
import logging

from cockpit.services.mpn_library.enrichment_plan import plan_enrichment
from cockpit.services.mpn_library.enrichment_worker import EnrichmentWorker
from cockpit.services.mpn_library.digikey_types import RequestBudget
from cockpit.services.library_errors import CredentialsNotConfigured

logger = logging.getLogger(__name__)

class LibrarySegment(QWidget):
    def __init__(self, library_module: MpnLibraryModule, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.library_module = library_module
        self._current_bom = None
        self._has_observed = False
        self._worker = None
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        header_layout = QHBoxLayout()
        self.summary_label = QLabel("Observation Summary: pending")
        header_layout.addWidget(self.summary_label)
        
        header_layout.addStretch()
        
        self.enrich_btn = QPushButton("Enrich from DigiKey")
        self.enrich_btn.clicked.connect(self._on_enrich_clicked)
        header_layout.addWidget(self.enrich_btn)
        
        layout.addLayout(header_layout)
        
        self.grid = LibraryGridWidget(self.library_module.repository, utcnow, self)
        layout.addWidget(self.grid)

    def load(self, view: Any, bom_lines: list) -> None:
        """
        Called when an audit is loaded.
        """
        self._current_bom = bom_lines
        self._has_observed = False
        self.grid.set_parts([])
        self.summary_label.setText("Observation Summary: waiting for view")
        if self.isVisible():
            self._observe_and_render()

    def unload(self) -> None:
        self._current_bom = None
        self._has_observed = False
        self.grid.set_parts([])
        self.summary_label.setText("No audit loaded.")
        if self._worker:
            self._worker.cancel()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._has_observed and self._current_bom is not None:
            self._observe_and_render()

    def _observe_and_render(self) -> None:
        if not self._current_bom:
            return
            
        try:
            summary = self.library_module.repository.observe_bom(self._current_bom, utcnow)
            self.summary_label.setText(
                f"Observed {len(self._current_bom)} BOM lines: "
                f"{summary.new_unresolved_count} new unresolved, "
                f"{summary.new_unkeyable_count} new unkeyable, "
                f"{summary.existing_count} existing."
            )
            
            cur = self.library_module.ui_conn.cursor()
            seen_raw = set(line.component_mpn for line in self._current_bom)
            
            parts = []
            if seen_raw:
                placeholders = ",".join("?" * len(seen_raw))
                cur.execute(
                    f"SELECT mpn_key, mpn_as_seen, resolution_status FROM library_part WHERE mpn_as_seen IN ({placeholders})",
                    list(seen_raw)
                )
                for row in cur.fetchall():
                    parts.append((
                        row["mpn_key"],
                        row["mpn_as_seen"],
                        row["resolution_status"],
                        row["resolution_status"] == 'UNKEYABLE'
                    ))
            
            self.grid.set_parts(parts)
            self._has_observed = True
        except Exception as e:
            logger.exception("Failed to observe BOM")
            self.summary_label.setText(f"Error observing BOM: {e}")

    def set_operation_in_flight(self, in_flight: bool) -> None:
        self.grid.set_operation_in_flight(in_flight)
        self.enrich_btn.setEnabled(not in_flight)

    def _on_enrich_clicked(self):
        if not self._current_bom:
            return
            
        from PyQt6.QtCore import QSettings
        settings = QSettings()
        client_id = settings.value("library/digikey_client_id", "", type=str)
        client_secret = settings.value("library/digikey_client_secret", "", type=str)
        
        if not client_id or not client_secret:
            QMessageBox.warning(self, "Credentials Required", "DigiKey credentials not configured. Please add them in Settings.")
            return
            
        from cockpit.services.mpn_library.digikey_types import DigiKeyCredentials
        creds = DigiKeyCredentials(client_id=client_id, client_secret=client_secret)
            
        budget = 150 # default budget
        
        cur = self.library_module.ui_conn.cursor()
        seen_raw = set(line.component_mpn for line in self._current_bom)
        library_rows = {}
        if seen_raw:
            placeholders = ",".join("?" * len(seen_raw))
            cur.execute(
                f"SELECT mpn_key, resolution_status FROM library_part WHERE mpn_as_seen IN ({placeholders})",
                list(seen_raw)
            )
            for row in cur.fetchall():
                library_rows[row["mpn_key"]] = {"resolution_status": row["resolution_status"]}
                
        plan = plan_enrichment(self._current_bom, library_rows, refresh_stale=False, budget=budget)
        
        if not plan.to_query:
            QMessageBox.information(self, "Enrichment", "No unresolved parts to query.")
            return
            
        msg = (
            f"Enrich {len(plan.to_query)} parts from DigiKey\n\n"
            f"  {len(plan.to_query)} will be looked up\n"
            f"  {len(plan.already_known)} already in the library\n"
            f"  {len(plan.suppressed)} previously not found (not re-queried)\n"
            f"  {len(plan.unkeyable)} cannot be keyed\n\n"
            f"Budget: {budget} requests.\n"
            f"Cockpit will send {len(plan.to_query)} manufacturer part numbers to api.digikey.com. "
            f"Nothing else is transmitted: no description, designators, quantity, job number, assembly number, or customer."
        )
        
        reply = QMessageBox.question(self, "Confirm Enrichment", msg, QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if reply != QMessageBox.StandardButton.Ok:
            return
            
        self.set_operation_in_flight(True)
        self.summary_label.setText("Enriching...")
        
        self._worker = EnrichmentWorker(
            plan=plan,
            credentials=creds,
            budget=RequestBudget(remaining=budget),
            library_path=self.library_module.repository._conn._path, # using path to open new conn
            utcnow=utcnow
        )
        self._worker.part_enriched.connect(self.grid.update_part)
        self._worker.finished_report.connect(self._on_enrich_finished)
        self._worker.failed.connect(self._on_enrich_failed)
        self._worker.start()

    def _on_enrich_finished(self, report):
        self.set_operation_in_flight(False)
        self.summary_label.setText(f"Enrichment finished. {len(report.resolved)} resolved, {len(report.ambiguous)} ambiguous, {len(report.not_found)} not found.")
        self._worker.deleteLater()
        self._worker = None
        self._observe_and_render() # refresh

    def _on_enrich_failed(self, exception):
        self.set_operation_in_flight(False)
        self.summary_label.setText("Enrichment failed.")
        QMessageBox.critical(self, "Enrichment Failed", str(exception))
        self._worker.deleteLater()
        self._worker = None
