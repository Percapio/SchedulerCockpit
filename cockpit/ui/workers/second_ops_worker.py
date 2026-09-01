import pathlib

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from ...services.second_ops import (
    ReadFailureCause, SecondOpsRow, read_second_ops_rows
)

class SecondOpsReadWorker(QObject):
    rows_ready = pyqtSignal(object)  # list[SecondOpsRow]
    read_failed = pyqtSignal(object) # ReadFailureCause

    def __init__(self, workbook_path: pathlib.Path, terms: tuple[str, ...], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._workbook_path = workbook_path
        self._terms = terms

    @pyqtSlot()
    def run(self) -> None:
        result = read_second_ops_rows(self._workbook_path, self._terms)
        
        # Result unwrapping
        if isinstance(result, ReadFailureCause):
            self.read_failed.emit(result)
        else:
            self.rows_ready.emit(result)
