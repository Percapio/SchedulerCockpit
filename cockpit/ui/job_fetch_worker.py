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
