from typing import Callable

from PyQt6.QtCore import QObject, pyqtSignal, QSettings, QTimer
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

    def __init__(
        self,
        app: QApplication,
        theme: Theme,
        settings: QSettings,
        composer: Callable[[int], str] | None = None,
    ) -> None:
        super().__init__()
        self._app = app
        self._theme = theme
        self._settings = settings
        # Phase 32 (2.4): optional stylesheet composer so preset/font overrides
        # survive font-scale changes. Defaults to the plain theme stylesheet.
        self._composer = composer or theme.qss
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
        self._restyle_running: bool = False
        self._restyle_timer: QTimer = QTimer(self)
        self._restyle_timer.setSingleShot(True)
        self._restyle_timer.timeout.connect(self._run_restyle)

    def current_pt(self) -> int:
        return self._current_pt

    def set_pt(self, pt: int) -> None:
        new_pt: int = self._clamp(pt)
        if new_pt == self._current_pt:
            return
        self._current_pt = new_pt
        self._settings.setValue("audit_view/font_scale_pt", new_pt)
        self.scale_changed.emit(new_pt)
        self._schedule_restyle()

    def request_delta(self, delta_steps: int) -> None:
        """Move the current scale by delta_steps steps (NOT pt)."""
        self.set_pt(self._current_pt + delta_steps * self._bounds.step_pt)

    def _clamp(self, candidate_pt: int) -> int:
        return max(self._bounds.min_pt, min(self._bounds.max_pt, candidate_pt))

    def _schedule_restyle(self) -> None:
        if self._restyle_running or self._restyle_timer.isActive():
            return
        self._restyle_timer.start(0)

    def _run_restyle(self) -> None:
        if self._restyle_running:
            return
        self._restyle_running = True
        applied_pt: int = self._current_pt
        try:
            self._app.setStyleSheet(self._composer(applied_pt))
        finally:
            self._restyle_running = False
        if self._current_pt != applied_pt:
            self._schedule_restyle()

    def reapply(self) -> None:
        """Regenerate the stylesheet at the current scale (e.g. preset change)."""
        self._restyle_timer.stop()
        self._run_restyle()
