"""*Placements report…* (Phase 57 §10.5): what the audit's BOM Builder file gave, from stored rows only.

Modeless, one per audit; MainWindow tracks it as it tracks the BOM cleaning report. Every text is
plain text: a file name or a reason a newer Cockpit wrote is never read as markup.
"""

from __future__ import annotations

from datetime import datetime

from PyQt6.QtCore import Qt
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

from cockpit.placements.registration import Registered
from cockpit.placements.wording import reason_text, refusal_text
from cockpit.services.placements import PlacementsReport

NOTHING_RECORDED = "No BOM Builder file has been read for this audit."
SIDE_NAMES = {"T": "Top", "B": "Bottom"}
LIST_LIMIT = 40


def _local(instant: datetime | None) -> str:
    if instant is None:
        return "unknown"
    return instant.astimezone().strftime("%d %b %Y %H:%M")


def _from_ns(ns: int | None) -> str:
    if not ns:
        return "unknown"
    return _local(datetime.fromtimestamp(ns / 1e9).astimezone())


def _listing(designators: tuple[str, ...]) -> str:
    shown = ", ".join(designators[:LIST_LIMIT])
    if len(designators) > LIST_LIMIT:
        shown += f", and {len(designators) - LIST_LIMIT:,} more"
    return shown


def _plain_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    for column in range(len(headers) - 1):
        table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(len(headers) - 1, QHeaderView.ResizeMode.Stretch)
    table.verticalHeader().setVisible(False)
    table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    return table


def summary_lines(report: PlacementsReport) -> list[str]:
    """The report's text above its tables, in reading order."""
    if report.nothing_recorded:
        return [NOTHING_RECORDED]
    lines: list[str] = []
    if report.file is not None:
        size = f"{report.file.share_size:,} bytes" if report.file.share_size else "size unknown"
        lines.append(
            f"File used: {report.file.name} ({size}, modified {_from_ns(report.file.share_modified_ns)}), "
            f"read {_local(report.file.read_at)}"
        )
        lines.append(f"Placed: {report.placed:,} of {report.bom_designators:,} BOM designators")
        if report.not_placed:
            lines.append(f"Not placed by the file ({len(report.not_placed):,}): {_listing(report.not_placed)}")
        if report.marked_unstuffed:
            lines.append(
                f"Marked unstuffed in the file but in the BOM ({len(report.marked_unstuffed):,}): "
                f"{_listing(report.marked_unstuffed)}"
            )
    else:
        lines.append("No BOM Builder file is in use for this audit.")
    if report.newest_file_not_used:
        lines.append(f"The newest file wasn't used: {reason_text(report.rejections[0].reason)}")
    return lines


class PlacementsReportDialog(QDialog):
    def __init__(self, audit_id: int, report: PlacementsReport, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # Patch 14 §7.3: modeless and parented, so without this every closed report (and its data) lived
        # until the window did. Qt 6.3+ deletes on every way the dialog closes, Escape and reject() included.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._audit_id = audit_id
        self.setWindowTitle("Placements Report")
        self.setModal(False)
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        self.summary_label = _plain_label("\n".join(summary_lines(report)))
        layout.addWidget(self.summary_label)

        self.pages_table = _table(["Page", "Outcome", "Side", "Mirrored", "Labels", "Inliers", "p90 (mil)", "Reasons"])
        self.pages_table.setRowCount(len(report.pages))
        for row, page in enumerate(report.pages):
            if isinstance(page, Registered):
                reg = page.registration
                cells = [
                    str(reg.page_index + 1), "Registered", SIDE_NAMES.get(reg.side, reg.side),
                    "yes" if reg.board_to_page.mirrored else "no", str(reg.matched_labels), str(reg.inliers),
                    f"{reg.residual_p90_mils:.2f}", "",
                ]
            else:
                cells = [
                    str(page.page_index + 1), "Refused", SIDE_NAMES.get(page.side or "", "mixed or unknown"),
                    "", str(page.matched_labels), "", "", "; ".join(refusal_text(r) for r in page.reasons),
                ]
            for column, text in enumerate(cells):
                self.pages_table.setItem(row, column, QTableWidgetItem(text))
        self.pages_heading = _plain_label("Drawing pages")
        layout.addWidget(self.pages_heading)
        layout.addWidget(self.pages_table)
        self.pages_heading.setVisible(bool(report.pages))
        self.pages_table.setVisible(bool(report.pages))

        self.rejections_table = _table(["Seen", "File", "Reason"])
        self.rejections_table.setRowCount(len(report.rejections))
        for row, rejection in enumerate(report.rejections):
            for column, text in enumerate([
                _local(rejection.seen_at), rejection.share_file_name, reason_text(rejection.reason),
            ]):
                self.rejections_table.setItem(row, column, QTableWidgetItem(text))
        self.rejections_heading = _plain_label("Files not used, latest first")
        layout.addWidget(self.rejections_heading)
        layout.addWidget(self.rejections_table)
        self.rejections_heading.setVisible(bool(report.rejections))
        self.rejections_table.setVisible(bool(report.rejections))

        layout.addStretch()
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

    @property
    def audit_id(self) -> int:
        return self._audit_id
