"""Ingestion worker for background execution."""

import logging
import pathlib
from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal

from cockpit.ingestion.service import IngestionService
from cockpit.ingestion.progress import ProgressEvent, IngestionCancelled
from cockpit.ui.error_messages import render, FailurePayload


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuditSummary:
    audit_id: int
    part_number: str
    work_order_ref: str
    tht_item_count: int
    eco_item_count: int


class IngestionWorker(QObject):
    progress_signal = pyqtSignal(str)
    succeeded_signal = pyqtSignal(object)
    failed_signal = pyqtSignal(object)
    cancelled_signal = pyqtSignal()

    def __init__(self, service: IngestionService, paths: list[pathlib.Path]) -> None:
        super().__init__()
        self._service = service
        self._paths = paths
        self._cancel_requested = False
        self._last_detail = {}

    def run(self) -> None:
        """Run IngestionService.ingest on the calling thread."""
        try:
            audit = self._service.ingest(self._paths, progress=self._on_progress)
            
            assert "tht_item_count" in self._last_detail, "tht_item_count missing from progress stream"
            assert "eco_item_count" in self._last_detail, "eco_item_count missing from progress stream"
            
            summary = AuditSummary(
                audit_id=audit.id,
                part_number=audit.part_number,
                work_order_ref=audit.work_order_ref,
                tht_item_count=self._last_detail["tht_item_count"],
                eco_item_count=self._last_detail["eco_item_count"]
            )
            self.succeeded_signal.emit(summary)
            
        except IngestionCancelled:
            logger.exception('Exception caught in ingestion_worker')
            self.cancelled_signal.emit()
            
        except Exception as exc:
            logger.exception('Exception caught in ingestion_worker')
            payload = render(exc)
            logger.error("Ingest failed", exc_info=True)
            self.failed_signal.emit(payload)

    def request_cancel(self) -> None:
        """Invoked directly from the UI thread."""
        self._cancel_requested = True

    def _on_progress(self, event: ProgressEvent) -> None:
        """Progress callback invoked by IngestionService."""
        
        if self._cancel_requested:
            raise IngestionCancelled("User requested cancellation")
            
        if event.detail:
            self._last_detail.update(event.detail)
            
        self.progress_signal.emit(event.stage.value)
