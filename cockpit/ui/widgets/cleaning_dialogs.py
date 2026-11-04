"""Dialogs and worker for Phase 53 BOM cleaning reports."""

import pathlib
from typing import Sequence

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cockpit.ingestion.errors import MalformedBomError
from cockpit.ingestion.parsers.audit_bom import (
    CleaningAction,
    CleaningReport,
    CleaningSummary,
    DesignatorPlaceholder,
    DesignatorRepeatDropped,
    DesignatorsStruck,
    EnclosingQuotesRemoved,
    FreeTextInRefDes,
    HiddenSheetRead,
    LineRemovedByStrike,
    MountTypeReplacedByStrike,
    NoteExtracted,
    PartNumberReplacedByStrike,
    StruckTextRemoved,
    UnknownMountTypeSkipped,
    build_cleaning_report,
    mount_gate_actions,
    read_cleaned_sheet,
)
from cockpit.persistence.repositories.source_files import SourceFileRepository
from cockpit.services.second_ops import ReadFailureCause, resolve_bom_workbook
from cockpit.ui.widgets.qt_lifecycle import disconnect_all_signals_quietly


def describe_cleaning_action(action: CleaningAction) -> tuple[str, str]:
    """Formats a CleaningAction as (row_text, description_text) in plain text."""
    if isinstance(action, HiddenSheetRead):
        return ("\u2014", f"The BOM was read from hidden sheet {action.sheet_name}.")
    if isinstance(action, LineRemovedByStrike):
        return (
            str(action.sheet_row),
            "The whole line is struck through and was left out.",
        )
    if isinstance(action, PartNumberReplacedByStrike):
        return (
            str(action.sheet_row),
            f"PartNum {action.struck} is struck through; read as {action.kept}.",
        )
    if isinstance(action, MountTypeReplacedByStrike):
        return (
            str(action.sheet_row),
            f"{action.struck} is struck through; read as {action.kept.value}.",
        )
    if isinstance(action, DesignatorsStruck):
        return (
            str(action.sheet_row),
            f"Struck designators removed: {', '.join(action.struck)}.",
        )
    if isinstance(action, DesignatorPlaceholder):
        return (
            str(action.sheet_row),
            f"Ref_Des placeholder {action.text} treated as no designators.",
        )
    if isinstance(action, FreeTextInRefDes):
        return (
            str(action.sheet_row),
            f"Free text in Ref_Des ({action.text}) kept as a note; no designators.",
        )
    if isinstance(action, UnknownMountTypeSkipped):
        if not action.mount_text:
            return (str(action.sheet_row), "No SMT/THT value; the part was left out.")
        return (
            str(action.sheet_row),
            f"Unrecognised SMT/THT value ({action.mount_text}); the part was left out.",
        )
    if isinstance(action, NoteExtracted):
        return (
            str(action.note.sheet_row),
            f"Note extracted from {action.note.column.value}: {action.note.text}",
        )
    if isinstance(action, EnclosingQuotesRemoved):
        return (
            str(action.sheet_row),
            f"Enclosing quotes removed in {action.column.value}.",
        )
    if isinstance(action, StruckTextRemoved):
        return (
            str(action.sheet_row),
            f"Struck-through text removed in {action.column.value}.",
        )
    if isinstance(action, DesignatorRepeatDropped):
        return (
            str(action.sheet_row),
            f"Repeated designator {action.designator} dropped.",
        )
    return (str(getattr(action, "sheet_row", "\u2014")), str(action))


class CleaningChangesDialog(QDialog):
    """Modal informational dialog listing consequential BOM cleaning changes after ingest."""

    def __init__(
        self,
        summary: CleaningSummary,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setWindowTitle("BOM Cleaning Changes")
        self.resize(560, 360)

        # Hold an immutable copy so nothing can change underneath the dialog.
        self._consequential: tuple[CleaningAction, ...] = tuple(summary.consequential)
        self._full_report_requested = False

        layout = QVBoxLayout(self)

        self.intro_label = QLabel("The following BOM changes were applied during ingestion:")
        self.intro_label.setTextFormat(Qt.TextFormat.PlainText)
        self.intro_label.setWordWrap(True)
        layout.addWidget(self.intro_label)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Row", "What happened"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        self._populate_table(self._consequential)
        layout.addWidget(self.table)

        btn_layout = QHBoxLayout()
        self.full_report_btn = QPushButton("Full report\u2026")
        self.full_report_btn.clicked.connect(self._on_full_report_clicked)
        btn_layout.addWidget(self.full_report_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.setDefault(True)
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _populate_table(self, actions: Sequence[CleaningAction]) -> None:
        self.table.setRowCount(len(actions))
        for row_idx, action in enumerate(actions):
            row_str, desc_str = describe_cleaning_action(action)
            self.table.setItem(row_idx, 0, QTableWidgetItem(row_str))
            self.table.setItem(row_idx, 1, QTableWidgetItem(desc_str))

    def _on_full_report_clicked(self) -> None:
        self._full_report_requested = True
        self.accept()

    def full_report_requested(self) -> bool:
        return self._full_report_requested


class CleaningReportWorker(QObject):
    """Background worker that reads and builds the on-demand BOM cleaning report."""

    report_ready = pyqtSignal(object)
    read_failed = pyqtSignal(str)

    def __init__(self, path: pathlib.Path) -> None:
        super().__init__()
        self._path = path

    def run(self) -> None:
        try:
            sheet = read_cleaned_sheet(self._path)
            report = build_cleaning_report(sheet, mount_gate_actions(sheet))
        except MalformedBomError as exc:
            self.read_failed.emit(exc.reason)
            return
        except Exception as exc:
            self.read_failed.emit(str(exc))
            return
        self.report_ready.emit(report)


# Anchors in-flight report threads past the dialog's own lifetime.
_REPORT_READS_IN_FLIGHT: set[tuple[QThread, CleaningReportWorker]] = set()


class CleaningReportDialog(QDialog):
    """On-demand full BOM cleaning report dialog."""

    def __init__(
        self,
        audit_id: int,
        source_file_repo: SourceFileRepository,
        parent: QWidget | None = None,
        *,
        auto_start: bool = True,
    ) -> None:
        super().__init__(parent)
        self._audit_id = audit_id
        self._source_file_repo = source_file_repo
        self._thread: QThread | None = None
        self._worker: CleaningReportWorker | None = None

        self.setWindowTitle("BOM Cleaning Report")
        self.resize(640, 480)

        layout = QVBoxLayout(self)

        self.header_label = QLabel("Reading the stored BOM\u2026")
        self.header_label.setTextFormat(Qt.TextFormat.PlainText)
        self.header_label.setWordWrap(True)
        layout.addWidget(self.header_label)

        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Row", "What happened"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.hide()
        layout.addWidget(self.table)

        self.rejected_heading = QLabel("Rows rejected by current rules")
        self.rejected_heading.setTextFormat(Qt.TextFormat.PlainText)
        self.rejected_heading.hide()
        layout.addWidget(self.rejected_heading)

        self.rejected_table = QTableWidget()
        self.rejected_table.setColumnCount(3)
        self.rejected_table.setHorizontalHeaderLabels(["Row", "Ref_Des", "Rejected token"])
        self.rejected_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.rejected_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.rejected_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.rejected_table.verticalHeader().setVisible(False)
        self.rejected_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.rejected_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.rejected_table.hide()
        layout.addWidget(self.rejected_table)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

        resolved = resolve_bom_workbook(self._audit_id, self._source_file_repo)
        if isinstance(resolved, ReadFailureCause):
            self._on_read_failed(resolved.value)
        elif auto_start:
            self._start_worker(resolved)

    def _start_worker(self, path: pathlib.Path) -> None:
        self._detach_worker()
        self.header_label.setText("Reading the stored BOM\u2026")
        self.table.hide()
        self.rejected_heading.hide()
        self.rejected_table.hide()

        thread = QThread()
        worker = CleaningReportWorker(path)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.report_ready.connect(self._on_report_ready)
        worker.read_failed.connect(self._on_read_failed)

        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        pair = (thread, worker)
        _REPORT_READS_IN_FLIGHT.add(pair)
        thread.finished.connect(lambda p=pair: _REPORT_READS_IN_FLIGHT.discard(p))

        self._thread = thread
        self._worker = worker
        thread.start()

    def _detach_worker(self) -> None:
        worker, thread = self._worker, self._thread
        self._worker = None
        self._thread = None

        if worker is not None:
            disconnect_all_signals_quietly(worker)
        if thread is not None:
            try:
                thread.quit()
            except RuntimeError:
                pass

    def reject(self) -> None:
        self._detach_worker()
        super().reject()

    def closeEvent(self, event) -> None:
        self._detach_worker()
        super().closeEvent(event)

    def _on_report_ready(self, report: CleaningReport) -> None:
        self._detach_worker()
        hidden_suffix = " (hidden)" if not report.sheet_is_visible else ""
        self.header_label.setText(
            f"Cleaning rules version {report.rules_version} \u00b7 sheet {report.sheet_name}{hidden_suffix}, header row {report.header_row}"
        )

        self.table.setRowCount(len(report.actions))
        for idx, action in enumerate(report.actions):
            row_str, desc_str = describe_cleaning_action(action)
            self.table.setItem(idx, 0, QTableWidgetItem(row_str))
            self.table.setItem(idx, 1, QTableWidgetItem(desc_str))
        self.table.show()

        if report.rejected_rows:
            self.rejected_table.setRowCount(len(report.rejected_rows))
            for idx, rej in enumerate(report.rejected_rows):
                self.rejected_table.setItem(idx, 0, QTableWidgetItem(str(rej.sheet_row)))
                self.rejected_table.setItem(idx, 1, QTableWidgetItem(rej.cell_text))
                self.rejected_table.setItem(idx, 2, QTableWidgetItem(rej.token))
            self.rejected_heading.show()
            self.rejected_table.show()
        else:
            self.rejected_heading.hide()
            self.rejected_table.hide()

    def _on_read_failed(self, reason: str) -> None:
        self._detach_worker()
        self.header_label.setText(f"The stored BOM couldn't be read: {reason}")
        self.table.hide()
        self.rejected_heading.hide()
        self.rejected_table.hide()
