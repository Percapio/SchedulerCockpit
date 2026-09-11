"""Job fetch and selection dialogs."""

import pathlib
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLineEdit, QPushButton, QLabel, 
    QRadioButton, QGroupBox, QButtonGroup, QScrollArea, QWidget
)
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtCore import QRegularExpression, Qt

from cockpit.ingestion.locator import PendingSelection, SourceRole
from PyQt6.QtCore import pyqtSignal

class FetchJobDialog(QDialog):
    fetch_requested = pyqtSignal(str)

    def __init__(self, last_job_number: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Fetch Job")
        self.setModal(True)
        self.setMinimumWidth(300)
        
        layout = QVBoxLayout(self)
        
        layout.addWidget(QLabel("Job Number:"))
        
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(last_job_number)
        
        # Mask: 1 upper case letter, 6 digits
        validator = QRegularExpressionValidator(QRegularExpression(r"^[a-zA-Z][0-9]{6}$"))
        self.edit.setValidator(validator)
        layout.addWidget(self.edit)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.clicked.connect(self._on_fetch_clicked)
        
        # Disable Fetch if edit is empty. (Placeholder is not committed text)
        self.fetch_btn.setEnabled(False)
        self.edit.textChanged.connect(self._on_text_changed)
        
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(self.cancel_btn)
        btn_layout.addWidget(self.fetch_btn)
        
        layout.addLayout(btn_layout)
        self._is_fetching = False

    def _on_fetch_clicked(self) -> None:
        self.set_fetching_state(True)
        self.fetch_requested.emit(self.job_number())

    def _on_text_changed(self, text: str) -> None:
        if self._is_fetching:
            return
        # Re-evaluate without strict state so we can type "B142..."
        # But we only enable if length is 7
        self.fetch_btn.setEnabled(len(text.strip()) == 7)

    def set_fetching_state(self, is_fetching: bool) -> None:
        self._is_fetching = is_fetching
        self.edit.setEnabled(not is_fetching)
        self.fetch_btn.setEnabled(not is_fetching)
        if is_fetching:
            self.cancel_btn.setText("Stop waiting")
        else:
            self.cancel_btn.setText("Cancel")
            self._on_text_changed(self.edit.text())

    def job_number(self) -> str:
        return self.edit.text().strip().upper()


class SelectSourcesDialog(QDialog):
    def __init__(self, pending: PendingSelection, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Source Files")
        self.setModal(True)
        self.setMinimumWidth(500)
        
        self.pending = pending
        self.choices = {}
        
        layout = QVBoxLayout(self)
        
        info = QLabel(f"Job {pending.job_number} has multiple files for some roles.\nPlease choose the correct file for each.")
        layout.addWidget(info)
        
        if pending.ignored_count > 0:
            ignored_lbl = QLabel(f"Ignored {pending.ignored_count} unrecognized file(s).")
            ignored_lbl.setStyleSheet("color: gray;")
            layout.addWidget(ignored_lbl)
            
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        
        # Display resolved first
        if pending.resolved:
            resolved_group = QGroupBox("Already matched (Read-only)")
            res_layout = QVBoxLayout(resolved_group)
            for role, path in pending.resolved.items():
                lbl = QLabel(f"<b>{role.value}:</b> {path.name}")
                res_layout.addWidget(lbl)
            scroll_layout.addWidget(resolved_group)
            
        # Display pending sets
        self.button_groups = {}
        for cset in pending.sets:
            group = QGroupBox(f"Choose {cset.role.value}")
            g_layout = QVBoxLayout(group)
            
            bg = QButtonGroup(self)
            self.button_groups[cset.role] = bg
            
            for cand in cset.candidates:
                rb = QRadioButton(cand.name)
                bg.addButton(rb)
                g_layout.addWidget(rb)
                
                # Check preselected
                if cset.preselected == cand:
                    rb.setChecked(True)
                    self.choices[cset.role] = cand
                    
                rb.toggled.connect(lambda checked, r=cset.role, c=cand: self._on_choice_toggled(checked, r, c))
                
            scroll_layout.addWidget(group)
            
        scroll_layout.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll)
        
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.accept_btn = QPushButton("Accept")
        self.accept_btn.clicked.connect(self.accept)
        
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        
        btn_layout.addWidget(cancel_btn)
        btn_layout.addWidget(self.accept_btn)
        layout.addLayout(btn_layout)
        
        self._update_accept_btn()
        
    def _on_choice_toggled(self, checked: bool, role: SourceRole, cand: pathlib.Path) -> None:
        if checked:
            self.choices[role] = cand
        self._update_accept_btn()
        
    def _update_accept_btn(self) -> None:
        # Check if all roles have a choice
        for cset in self.pending.sets:
            if cset.role not in self.choices:
                self.accept_btn.setEnabled(False)
                return
        self.accept_btn.setEnabled(True)
