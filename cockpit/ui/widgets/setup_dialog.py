from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QComboBox, QFormLayout, QDialogButtonBox
)
from cockpit.layout.constants import PAGE_SIDE_LABELS
from cockpit.services.setup_bom import SideFilter, ProcessFilter

class SetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Setup BOM Print")
        
        layout = QVBoxLayout(self)
        form = QFormLayout()
        
        self.side_combo = QComboBox()
        self.side_combo.addItems([SideFilter.TOP, SideFilter.BOTTOM, SideFilter.BOTH])
        form.addRow("Side:", self.side_combo)
        
        self.process_combo = QComboBox()
        self.process_combo.addItems([ProcessFilter.SMT, ProcessFilter.THT, ProcessFilter.BOTH])
        form.addRow("Process:", self.process_combo)
        
        layout.addLayout(form)
        
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def get_filters(self) -> tuple[str, str]:
        return self.side_combo.currentText(), self.process_combo.currentText()
