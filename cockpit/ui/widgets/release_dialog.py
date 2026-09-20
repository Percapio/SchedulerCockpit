from enum import Enum

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QFormLayout, QDialogButtonBox, QCheckBox
)
from cockpit.services.release import ReleaseFormData
from cockpit.persistence.types import AuditStatus


class DetailsOutcome(Enum):
    """Which button closed the modal.

    Update no longer closes, so it is not an outcome: it commits in place and
    the dialog stays open. What remains is whether the operator asked to print
    on the way out.
    """

    RELEASE = "RELEASE"
    CLOSED = "CLOSED"


def resolve_ship_date(raw_ship_date: str, blank_requested: bool):
    """Resolves the ship-date control into what should be persisted.

    pre:  blank_requested reflects the Blank checkbox
    post: None iff blank_requested; otherwise the parsed date
    raises: ValueError when a non-blank control yields an unparseable value,
            which is a programming error and must not silently reach the
            database as a NULL that clears a date nobody asked to clear
    """
    from datetime import date

    if blank_requested:
        return None
    text = (raw_ship_date or "").strip()
    if not text:
        return None
    return date.fromisoformat(text)

from PyQt6.QtCore import Qt, QDate, pyqtSignal
from cockpit.layout.constants import PAGE_SIDE_LABELS

class ReleaseDialog(QDialog):
    # The dialog holds no service. It asks to be committed and is told what
    # happened, which is what keeps it constructible with nothing injected.
    update_requested = pyqtSignal()

    def __init__(self, initial_data: ReleaseFormData, initial_status: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Details")
        self.setMinimumWidth(400)
        
        self.initial_data = initial_data
        self._outcome = DetailsOutcome.CLOSED
        
        layout = QVBoxLayout(self)
        
        form = QFormLayout()
        
        # Status
        self.status_combo = QComboBox()
        for status in AuditStatus:
            self.status_combo.addItem(status.value)
        self.status_combo.setCurrentText(initial_status)
        form.addRow("Workflow Status:", self.status_combo)
        
        # Auto fields (editable)
        self.assembly_input = QLineEdit(initial_data.assembly_number or "")
        form.addRow("B#:", self.assembly_input)
        
        self.qty_input = QLineEdit(str(initial_data.quantity) if initial_data.quantity is not None else "")
        form.addRow("Quantity:", self.qty_input)
        
        self.lead_time_input = QLineEdit(str(initial_data.lead_time_days) if initial_data.lead_time_days is not None else "")
        form.addRow("LT:", self.lead_time_input)
        
        self.repeat_input = QLineEdit(initial_data.repeat)
        form.addRow("Type:", self.repeat_input)
        
        self.itar_input = QLineEdit(initial_data.itar_display)
        form.addRow("ITAR:", self.itar_input)
        
        self.clean_input = QLineEdit(initial_data.process_clean or "")
        form.addRow("Clean:", self.clean_input)
        
        self.class_input = QLineEdit(initial_data.class_display)
        form.addRow("Class:", self.class_input)
        
        self.process_input = QLineEdit(initial_data.process or "")
        form.addRow("Process:", self.process_input)
        
        # Manual fields
        from PyQt6.QtWidgets import QDateEdit
        self.ship_date_input = QDateEdit()
        self.ship_date_input.setCalendarPopup(True)
        if initial_data.ship_date:
            try:
                self.ship_date_input.setDate(QDate.fromString(initial_data.ship_date, Qt.DateFormat.ISODate))
            except Exception:
                self.ship_date_input.setDate(QDate.currentDate())
        else:
            self.ship_date_input.setDate(QDate.currentDate())
        
        self.ship_date_blank_check = QCheckBox("Blank")
        self.ship_date_blank_check.stateChanged.connect(
            lambda state: self.ship_date_input.setEnabled(not state)
        )
        if not initial_data.ship_date:
            self.ship_date_blank_check.setChecked(True)
            self.ship_date_input.setEnabled(False)

        self.turn_note_input = QLineEdit()
        form.addRow("HOT JOB:", self.turn_note_input)

        ship_date_layout = QHBoxLayout()
        ship_date_layout.addWidget(self.ship_date_input)
        ship_date_layout.addWidget(self.ship_date_blank_check)
        form.addRow("Ship Date:", ship_date_layout)
        
        self.setup_side_combo = QComboBox()
        self.setup_side_combo.addItems(PAGE_SIDE_LABELS)
        form.addRow("1st Setup Side:", self.setup_side_combo)
        # PCB Clear composer
        self.pcb_date_input = QLineEdit()
        form.addRow("PCB Clear Date:", self.pcb_date_input)

        self.shortages_notes_input = QLineEdit()
        form.addRow("Shortages Notes:", self.shortages_notes_input)

        self.program_check = QCheckBox("Program in Kit")
        form.addRow("", self.program_check)
        
        self.folder_check = QCheckBox("Folder in Kit")
        form.addRow("", self.folder_check)
        
        self.floor_notes_input = QLineEdit()
        form.addRow("Floor Notes:", self.floor_notes_input)

        layout.addLayout(form)

        notice = QLabel(
            "Only Workflow Status and Ship Date are saved. "
            "Other fields apply to the printed form only."
        )
        notice.setWordWrap(True)
        notice.setProperty("class", "hint")
        layout.addWidget(notice)

        # Says what the last commit did, since Update no longer closes the
        # modal and a vanishing button is a weak signal on its own.
        self.status_lbl = QLabel("")
        self.status_lbl.setWordWrap(True)
        layout.addWidget(self.status_lbl)

        btns = QDialogButtonBox()
        self.update_btn = btns.addButton("Update", QDialogButtonBox.ButtonRole.ApplyRole)
        self.release_btn = btns.addButton("Release", QDialogButtonBox.ButtonRole.AcceptRole)
        # Never "Cancel": it has never undone anything, and now that Update can
        # commit without closing, "Cancel" would actively mislead.
        self.close_btn = btns.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.update_btn.clicked.connect(self._on_update_clicked)
        self.release_btn.clicked.connect(self._on_release_clicked)
        self.close_btn.clicked.connect(self.reject)
        layout.addWidget(btns)

        # Recomputed on every change to a persistable widget, not only here: a
        # button that was correct when the dialog opened and wrong by the time
        # the operator reaches for it is worse than no button.
        # The baseline is what the form actually shows once populated, not what
        # it was asked to show. A status the combo could not match, or a ship
        # date that failed to parse, would otherwise register as an operator
        # edit on open and offer to commit a change nobody made.
        self._initial_status = self.status_combo.currentText()
        self._initial_ship_date = self._current_ship_date_text()

        self.status_combo.currentTextChanged.connect(self._refresh_update_visibility)
        self.ship_date_input.dateChanged.connect(self._refresh_update_visibility)
        self.ship_date_blank_check.toggled.connect(self._refresh_update_visibility)
        self._refresh_update_visibility()

    def has_persistable_change(self) -> bool:
        """Whether the modal holds an edit that Update can actually commit.

        post: true iff status or ship_date differ from what was loaded; an edit
              to any of the fifteen print-payload fields never makes this true
        """
        if self.status_combo.currentText() != self._initial_status:
            return True
        return self._current_ship_date_text() != self._initial_ship_date

    def _current_ship_date_text(self) -> str:
        if self.ship_date_blank_check.isChecked():
            return ""
        return self.ship_date_input.date().toString(Qt.DateFormat.ISODate)

    def _refresh_update_visibility(self) -> None:
        changed = self.has_persistable_change()
        self.update_btn.setVisible(changed)
        # The line describes the current form, not a commit two edits ago.
        if changed and self.status_lbl.text():
            self.status_lbl.setText("")

    def _on_update_clicked(self) -> None:
        """Asks to be committed. Does not close: this is an apply, not an OK."""
        self.update_requested.emit()

    def mark_committed(self, committed_status: str, committed_ship_date: str) -> None:
        """Rebases onto the values that were just persisted.

        post: the baseline becomes the committed values, so the existing
              visibility rule hides Update with no separate committed state
        """
        self._initial_status = committed_status
        self._initial_ship_date = committed_ship_date
        self.status_lbl.setText("Saved.")
        self.update_btn.setVisible(self.has_persistable_change())

    def mark_commit_failed(self, message: str) -> None:
        """Reports inline rather than raising a second dialog over this one.

        post: the baseline is unchanged, so Update stays visible to retry
        """
        self.status_lbl.setText(message)
        self.update_btn.setVisible(self.has_persistable_change())

    def _on_release_clicked(self) -> None:
        self._outcome = DetailsOutcome.RELEASE
        self.accept()

    def outcome(self) -> DetailsOutcome:
        return self._outcome

    def ship_date_is_blank(self) -> bool:
        return self.ship_date_blank_check.isChecked()

    def get_result(self) -> tuple[ReleaseFormData, str]:
        def parse_int(s):
            try: return int(s)
            except ValueError: return None

        pcb_clear_str = self.pcb_date_input.text()

        ship_date_str = "" if self.ship_date_blank_check.isChecked() else self.ship_date_input.date().toString(Qt.DateFormat.ISODate)

        data = ReleaseFormData(
            assembly_number=self.assembly_input.text(),
            quantity=parse_int(self.qty_input.text()),
            lead_time_days=parse_int(self.lead_time_input.text()),
            repeat=self.repeat_input.text(),
            assembly_modifier=self.initial_data.assembly_modifier,
            itar_display=self.itar_input.text(),
            process_clean=self.clean_input.text(),
            class_display=self.class_input.text(),
            process=self.process_input.text(),
            ship_date=ship_date_str,
            turn_note=self.turn_note_input.text(),
            floor_notes=self.floor_notes_input.text(),
            shortages_notes=self.shortages_notes_input.text(),
            pcb_clear=pcb_clear_str,
            setup_first_side=self.setup_side_combo.currentText(),
            program_in_kit=self.program_check.isChecked(),
            folder_in_kit=self.folder_check.isChecked()
        )
        return data, self.status_combo.currentText()
