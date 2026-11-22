"""The drawing worker for *Update ▸ Drawing* (Phase 57 §9.2).

MainWindow owns it, whichever call site starts it. It holds no connection: inputs are read on the
UI thread, and the plan goes back there to be applied.
"""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from cockpit.services.document_updates import _describe
from cockpit.services.drawing_update import (
    DrawingUnreadable,
    DrawingUpdateFailed,
    DrawingUpdateInputs,
    prepare_drawing_update,
)
from cockpit.ui.workers.share_watchdog import ShareCallBeacon, share_call_guard

logger = logging.getLogger(__name__)


class DrawingUpdateWorker(QObject):
    prepared = pyqtSignal(object)  # DrawingUpdatePlan
    failed = pyqtSignal(object)    # DrawingUpdateFailed

    def __init__(
        self,
        inputs: DrawingUpdateInputs,
        pdf_path: pathlib.Path,
        storage_root: pathlib.Path,
        layout_parser: Any,
        bom_parser: Any,
        run_id: str,
    ) -> None:
        super().__init__()
        self.inputs = inputs
        self.pdf_path = pdf_path
        self.storage_root = storage_root
        self.layout_parser = layout_parser
        self.bom_parser = bom_parser
        self.run_id = run_id
        self.stop = threading.Event()
        # Patch 14 §7.3: the worker's child, so moveToThread carries it and it dies with its worker.
        self.beacon = ShareCallBeacon(self)

    @pyqtSlot()
    def run(self) -> None:
        try:
            outcome = prepare_drawing_update(
                self.inputs, self.pdf_path, self.storage_root, self.layout_parser, self.bom_parser,
                share_call=share_call_guard(self.beacon), stop=self.stop, run_id=self.run_id,
            )
        except Exception as error:
            logger.exception("The drawing worker failed")
            outcome = DrawingUpdateFailed(DrawingUnreadable(_describe(error)), None)
        if isinstance(outcome, DrawingUpdateFailed):
            self.failed.emit(outcome)
        else:
            self.prepared.emit(outcome)
