from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QComboBox, QPushButton, QFormLayout, QDialogButtonBox, QCheckBox
)
from cockpit.services.release import ReleaseFormData
from cockpit.persistence.types import AuditStatus
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
        form.addRow("Assembly Number:", self.assembly_input)
        
        self.qty_input = QLineEdit(str(initial_data.quantity) if initial_data.quantity is not None else "")
        form.addRow("Quantity:", self.qty_input)
        
        self.lead_time_input = QLineEdit(str(initial_data.lead_time_days) if initial_data.lead_time_days is not None else "")
        form.addRow("Lead Time (days):", self.lead_time_input)
        
        self.repeat_input = QLineEdit(initial_data.repeat)
        form.addRow("Repeat:", self.repeat_input)
        
        self.itar_input = QLineEdit(initial_data.itar_display)
        form.addRow("ITAR:", self.itar_input)
        
        self.clean_input = QLineEdit(initial_data.process_clean or "")
        form.addRow("Clean:", self.clean_input)
        
        self.class_input = QLineEdit(initial_data.class_display)
        form.addRow("Class:", self.class_input)
        
        self.process_input = QLineEdit(initial_data.process or "")
        form.addRow("Process:", self.process_input)
        
        # Manual fields
        self.ship_date_input = QLineEdit()
        form.addRow("Ship Date:", self.ship_date_input)
        
        self.turn_note_input = QLineEdit()
        form.addRow("Turn Note:", self.turn_note_input)
        
        self.email_notes_input = QLineEdit()
        form.addRow("Email Notes:", self.email_notes_input)
        
        self.floor_notes_input = QLineEdit()
        form.addRow("Floor Notes:", self.floor_notes_input)
        
        self.shortages_notes_input = QLineEdit()
        form.addRow("Shortages Notes:", self.shortages_notes_input)
        
        # PCB Clear composer
        self.pcb_date_input = QLineEdit()
        self.pcb_clear_check = QCheckBox("Is Clear")
        pcb_layout = QHBoxLayout()
        pcb_layout.addWidget(self.pcb_date_input)
        pcb_layout.addWidget(self.pcb_clear_check)
        form.addRow("PCB Clear Date:", pcb_layout)
        
        self.setup_side_combo = QComboBox()
        self.setup_side_combo.addItems(PAGE_SIDE_LABELS)
        form.addRow("Setup First Side:", self.setup_side_combo)
        
        self.program_check = QCheckBox("Program in Kit")
        form.addRow("", self.program_check)
        
        self.folder_check = QCheckBox("Folder in Kit")
        form.addRow("", self.folder_check)
        
        layout.addLayout(form)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_result(self) -> tuple[ReleaseFormData, str]:
        def parse_int(s):
            try: return int(s)
            except ValueError: return None

        pcb_clear_str = f"{self.pcb_date_input.text()} is {'clear' if self.pcb_clear_check.isChecked() else 'not clear'}"
        if not self.pcb_date_input.text():
            pcb_clear_str = ""

        data = ReleaseFormData(
            assembly_number=self.assembly_input.text(),
            quantity=parse_int(self.qty_input.text()),
            lead_time_days=parse_int(self.lead_time_input.text()),
            repeat=self.repeat_input.text(),
            itar_display=self.itar_input.text(),
            process_clean=self.clean_input.text(),
            class_display=self.class_input.text(),
            process=self.process_input.text(),
            ship_date=self.ship_date_input.text(),
            turn_note=self.turn_note_input.text(),
            email_notes=self.email_notes_input.text(),
            floor_notes=self.floor_notes_input.text(),
            shortages_notes=self.shortages_notes_input.text(),
            pcb_clear=pcb_clear_str,
            setup_first_side=self.setup_side_combo.currentText(),
            program_in_kit=self.program_check.isChecked(),
            folder_in_kit=self.folder_check.isChecked()
        )
        return data, self.status_combo.currentText()
