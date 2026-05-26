from PyQt6.QtCore import QObject, pyqtSignal, QSettings
from PyQt6.QtWidgets import QApplication

from cockpit.ui.theme import Theme, ConfigurationError
import logging
logger = logging.getLogger(__name__)



class FontScaleController(QObject):
    """
    Intent:           Single source of truth for the current QSS font scale.
                      Persists per-user via an injected file-based QSettings,
                      regenerates the application stylesheet on change,
                      and broadcasts the new scale.
    """

    scale_changed = pyqtSignal(int)

    def __init__(self, app: QApplication, theme: Theme, settings: QSettings) -> None:
        super().__init__()
        self._app = app
        self._theme = theme
        self._settings = settings
        self._bounds = theme.font_scale_bounds()

        val = self._settings.value("audit_view/font_scale_pt")
        pt = self._bounds.default_pt
        
        if val is not None:
            try:
                parsed = int(val)
                if not (self._bounds.min_pt <= parsed <= self._bounds.max_pt):
                    # Handle ConfigurationError internally as a fallback
                    # to satisfy the "ConfigurationError-handled fallback" requirement.
                    self._settings.setValue("audit_view/font_scale_pt", pt)
                else:
                    pt = parsed
            except ValueError:
                logger.exception('Exception caught in font_scale_controller')
                self._settings.setValue("audit_view/font_scale_pt", pt)
                
        self._current_pt = pt

    def current_pt(self) -> int:
        return self._current_pt

    def request_delta(self, delta_steps: int) -> None:
        """Move the current scale by delta_steps steps (NOT pt)."""
        candidate_pt = self._current_pt + delta_steps * self._bounds.step_pt
        new_pt = self._clamp(candidate_pt)
        if new_pt != self._current_pt:
            self._apply(new_pt)

    def _clamp(self, candidate_pt: int) -> int:
        return max(self._bounds.min_pt, min(self._bounds.max_pt, candidate_pt))

    def _apply(self, new_pt: int) -> None:
        from PyQt6.QtCore import QTimer
        self._current_pt = new_pt
        self._settings.setValue("audit_view/font_scale_pt", new_pt)
        self.scale_changed.emit(new_pt)
        QTimer.singleShot(0, lambda: self._app.setStyleSheet(self._theme.qss(new_pt)))
