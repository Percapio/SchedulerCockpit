from PyQt6.QtWidgets import QWidget, QVBoxLayout
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPaintEvent
from PyQt6.QtCore import Qt

class ChamferedPane(QWidget):
    """
    A container that draws a 45-degree chamfered ground behind a single child.
    """
    def __init__(
        self,
        content: QWidget,
        chamfer_px: int,
        inset_px: int,
        fill_rgb: str
    ) -> None:
        super().__init__()
        self._chamfer_px = chamfer_px
        self._fill_color = QColor(fill_rgb)
        
        # Inset layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(inset_px, inset_px, inset_px, inset_px)
        layout.setSpacing(0)
        layout.addWidget(content)
        
    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._fill_color)
        
        w = float(self.width())
        h = float(self.height())
        c = float(min(self._chamfer_px, w / 2, h / 2))
        
        path = QPainterPath()
        path.moveTo(c, 0)
        path.lineTo(w - c, 0)
        path.lineTo(w, c)
        path.lineTo(w, h - c)
        path.lineTo(w - c, h)
        path.lineTo(c, h)
        path.lineTo(0, h - c)
        path.lineTo(0, c)
        path.closeSubpath()
        
        painter.drawPath(path)
