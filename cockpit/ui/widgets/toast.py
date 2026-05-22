"""Toast notification widget."""

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from cockpit.ui.workers.ingestion_worker import AuditSummary


class Toast(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Not a top-level window, so we don't set FramelessWindowHint
        
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(15, 15, 15, 15)
        
        self.title_label = QLabel()
        self.title_label.setObjectName("ToastTitle")
        
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("ToastSubtitle")
        
        self.layout.addWidget(self.title_label)
        self.layout.addWidget(self.subtitle_label)
        
        self.hide()
        
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def mousePressEvent(self, event) -> None:
        """Dismiss on click."""
        self.hide()
        super().mousePressEvent(event)

    def _position_bottom_right(self) -> None:
        if not self.parentWidget():
            return
            
        parent_rect = self.parentWidget().rect()
        # Ensure geometry is computed
        self.adjustSize()
        self.move(
            parent_rect.width() - self.width() - 20,
            parent_rect.height() - self.height() - 20
        )

    def show_success(self, summary: AuditSummary) -> None:
        """Show success toast."""
        self.title_label.setText(f"Ingested {summary.part_number} / {summary.work_order_ref}")
        self.subtitle_label.setText(f"{summary.tht_item_count} THT items, {summary.eco_item_count} ECO items")
        self.subtitle_label.show()
        
        self.setStyleSheet("""
            Toast {
                background-color: #e8f5e9;
                border: 1px solid #4caf50;
                border-radius: 8px;
            }
            QLabel#ToastTitle { color: #2e7d32; font-weight: bold; }
            QLabel#ToastSubtitle { color: #388e3c; }
        """)
        
        self._show_toast()

    def show_cancel(self) -> None:
        """Show cancellation toast."""
        self.title_label.setText("Ingest cancelled")
        self.subtitle_label.hide()
        
        self.setStyleSheet("""
            Toast {
                background-color: #fff3e0;
                border: 1px solid #ff9800;
                border-radius: 8px;
            }
            QLabel#ToastTitle { color: #e65100; font-weight: bold; }
        """)
        
        self._show_toast()

    def _show_toast(self) -> None:
        self._position_bottom_right()
        self.show()
        self.raise_()
        self._timer.start(3000)
