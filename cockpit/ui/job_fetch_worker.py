"""Worker for background fetching."""

import pathlib
from PyQt6.QtCore import QObject, pyqtSignal

from cockpit.ingestion.locator import locate, FetchOutcome
from cockpit.ui.error_messages import FailurePayload, render

class JobFetchWorker(QObject):
    succeeded_signal = pyqtSignal(object)  # FetchOutcome
    failed_signal = pyqtSignal(object)     # FailurePayload
    
    def __init__(self, job_number: str, source_root: pathlib.Path):
        super().__init__()
        self.job_number = job_number
        self.source_root = source_root
        
    def run(self) -> None:
        try:
            outcome = locate(self.job_number, self.source_root)
            self.succeeded_signal.emit(outcome)
        except Exception as e:
            self.failed_signal.emit(render(e))


class DrawingFetchWorker(QObject):
    """Locates a drawing off the share without touching the UI thread.

    The same abandonment contract as JobFetchWorker: a blocking stat on an
    unreachable share cannot be interrupted, so MainWindow's watchdog abandons
    this object rather than waiting on it.
    """

    succeeded_signal = pyqtSignal(object)  # DrawingFetchOutcome
    failed_signal = pyqtSignal(object)     # FailurePayload

    def __init__(self, job_number: str, source_root: pathlib.Path):
        super().__init__()
        self.job_number = job_number
        self.source_root = source_root

    def run(self) -> None:
        from cockpit.ingestion.locator import locate_drawing
        try:
            self.succeeded_signal.emit(locate_drawing(self.job_number, self.source_root))
        except Exception as e:
            self.failed_signal.emit(render(e))
