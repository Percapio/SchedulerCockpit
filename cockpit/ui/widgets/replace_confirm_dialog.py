"""Phase 51 section 3.4 — the replacement confirmation.

It enumerates what is destroyed rather than asking "Replace?". Everything it
lists is operator-entered or operator-accumulated state that a fresh ingest
cannot reproduce, and the heading names both article designations so that
replacing a 2nd article with an FA reads as that before it happens.
"""

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from cockpit.services.replacement import ReplacementCasualty


def _casualty_lines(casualty: ReplacementCasualty) -> list[str]:
    lines = [
        f"Quantity {casualty.quantity}",
        f"Status {casualty.status.value}",
    ]
    if casualty.ops_per_board_min is not None:
        lines.append(f"OPS per board {casualty.ops_per_board_min}")
    if casualty.is_labeled:
        lines.append("Labelled")
    if casualty.are_photos_uploaded:
        lines.append("Photos uploaded")
    if casualty.tht_item_count:
        lines.append(f"{casualty.tht_item_count} THT checklist items")
    return lines


class ReplaceConfirmDialog(QDialog):
    def __init__(
        self,
        part_number: str,
        work_order_ref: str,
        incoming_article: str,
        casualties: list[ReplacementCasualty],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Replace stored audit")
        self.setModal(True)
        self.setMinimumWidth(460)
        self._casualties = casualties

        layout = QVBoxLayout(self)

        outgoing = ", ".join(sorted({c.article_revision for c in casualties})) or "FA"
        heading = QLabel(
            f"<b>{part_number} / {work_order_ref}</b> is already stored as "
            f"<b>{outgoing}</b>. Ingesting this file set as <b>{incoming_article}</b> "
            f"will delete {'it' if len(casualties) == 1 else f'all {len(casualties)} of its rows'} "
            f"and everything below."
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        for casualty in casualties:
            entry = QLabel(
                f"<b>{casualty.label}</b><br>"
                + "<br>".join(f"&nbsp;&nbsp;{line}" for line in _casualty_lines(casualty))
            )
            entry.setWordWrap(True)
            body_layout.addWidget(entry)
        body_layout.addStretch()
        scroll.setWidget(body)
        layout.addWidget(scroll, stretch=1)

        warning = QLabel("This cannot be undone. The source files on the share are unaffected.")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        buttons = QDialogButtonBox()
        self.replace_btn = buttons.addButton("Replace", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.cancel_btn = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        # Cancel is the default: an irreversible delete should not be one
        # stray Return away.
        self.cancel_btn.setDefault(True)
        self.replace_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        layout.addWidget(buttons)
