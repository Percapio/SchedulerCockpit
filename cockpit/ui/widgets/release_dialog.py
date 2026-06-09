from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QFormLayout, QDialogButtonBox, QCheckBox
)
from cockpit.services.release import ReleaseFormData
from cockpit.persistence.types import AuditStatus

from PyQt6.QtCore import Qt, QDate
from cockpit.layout.constants import PAGE_SIDE_LABELS

class ReleaseDialog(QDialog):
    def __init__(self, initial_data: ReleaseFormData, initial_status: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Release Audit")
        self.setMinimumWidth(400)
        
        self.initial_data = initial_data
        
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
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

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
