"""ProgressView widget."""

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton, QHBoxLayout

from cockpit.ingestion.progress import ProgressStage


class ProgressView(QWidget):
    cancel_requested = pyqtSignal()

    def __init__(self, stages: list[ProgressStage], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.stages = stages
        
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        self.stage_labels = {}
        
        for stage in stages:
            row_layout = QHBoxLayout()
            icon_label = QLabel("○")
            icon_label.setObjectName("ProgressIcon")
            icon_label.setFixedWidth(20)
            
            text_label = QLabel(stage.value.replace("_", " ").title())
            text_label.setObjectName("ProgressText")
            
            row_layout.addWidget(icon_label)
            row_layout.addWidget(text_label)
            row_layout.addStretch()
            
            self.stage_labels[stage] = (icon_label, text_label)
            layout.addLayout(row_layout)
            
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self._on_cancel)
        
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.cancel_button)
        button_layout.addStretch()
        
        layout.addSpacing(20)
        layout.addLayout(button_layout)
        
        self.reset()

    def _on_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_requested.emit()

    def advance(self, stage: ProgressStage) -> None:
        """Mark stage as completed."""
        if stage in self.stage_labels:
            icon_label, text_label = self.stage_labels[stage]
            icon_label.setText("✓")
            icon_label.setStyleSheet("color: green;")
            text_label.setStyleSheet("font-weight: bold;")

    def reset(self) -> None:
        """Reset all stages to pending."""
        for icon_label, text_label in self.stage_labels.values():
            icon_label.setText("○")
            icon_label.setStyleSheet("color: gray;")
            text_label.setStyleSheet("font-weight: normal; color: gray;")
            
        self.cancel_button.setEnabled(True)
