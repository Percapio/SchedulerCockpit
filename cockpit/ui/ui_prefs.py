"""Persistent UI preferences (Phase 32, 2.4) and diagnostics export (3.2)."""

import logging
import pathlib
import zipfile
import math
from typing import Any

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import QApplication

from cockpit.ui import facelift
from cockpit.ui.theme import Theme
from cockpit.services.runtime_constants import RuntimeConstants

logger = logging.getLogger(__name__)

_PRESET_KEY = "ui/theme_preset"
_FONT_FAMILY_KEY = "ui/font_family"


class StyleController(QObject):
    """Single source of truth for theme preset + font family.

    Composes the application stylesheet as Theme.qss(pt) + facelift override,
    and persists choices via the injected file-based QSettings.
    """

    changed = pyqtSignal()

    def __init__(self, app: QApplication, theme: Theme, settings: QSettings) -> None:
        super().__init__()
        self._app = app
        self._theme = theme
        self._settings = settings

        preset = str(settings.value(_PRESET_KEY, facelift.DEFAULT_PRESET))
        self._preset = preset if preset in facelift.PRESETS else facelift.DEFAULT_PRESET
        self._font_family = str(settings.value(_FONT_FAMILY_KEY, "") or "")
        facelift.set_active_preset(self._preset)

    def preset(self) -> str:
        return self._preset

    def font_family(self) -> str:
        return self._font_family

    def compose(self, pt: int | None = None) -> str:
        """Full application stylesheet: schema theme + facelift override."""
        return (
            self._theme.qss(pt)
            + "\n"
            + facelift.compose_override_qss(self._preset, self._font_family or None)
        )

    def set_preset(self, preset: str) -> None:
        if preset not in facelift.PRESETS or preset == self._preset:
            return
        self._preset = preset
        self._settings.setValue(_PRESET_KEY, preset)
        facelift.set_active_preset(preset)
        self.changed.emit()

    def set_font_family(self, family: str) -> None:
        family = (family or "").strip()
        if family == self._font_family:
            return
        self._font_family = family
        self._settings.setValue(_FONT_FAMILY_KEY, family)
        self.changed.emit()


def export_diagnostics(config, target_zip: pathlib.Path) -> list[str]:
    """Package logs + crash reports into target_zip; returns archived names (3.2)."""
    candidates: list[pathlib.Path] = []

    log_dir = config.log_path.parent
    candidates.extend(sorted(log_dir.glob(config.log_path.name + "*")))

    crash_dir = config.file_storage_root.parent / "crash_reports"
    if crash_dir.is_dir():
        candidates.extend(sorted(crash_dir.glob("*.json")))

    fault_log = config.app_data_root.parent / "faulthandler.log"
    if fault_log.is_file():
        candidates.append(fault_log)

    archived: list[str] = []
    with zipfile.ZipFile(target_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in candidates:
            if not path.is_file():
                continue
            try:
                zf.write(path, arcname=path.name)
                archived.append(path.name)
            except OSError:
                logger.warning("Diagnostics export: could not read %s", path)

    return archived


_RUNTIME_KEYS = {
    "smt_placement_time_min":        "runtime_calc/smt_placement_time_min",
    "tht_placement_time_min":        "runtime_calc/tht_placement_time_min",
    "aoi_inspection_time_min":       "runtime_calc/aoi_inspection_time_min",
    "class_3_multiplier_aoi":        "runtime_calc/class_3_multiplier_aoi",
    "class_3_multiplier_tht":        "runtime_calc/class_3_multiplier_tht",
    "clean_process_multiplier_tht":  "runtime_calc/clean_process_multiplier_tht",
    "clean_process_multiplier_ops":  "runtime_calc/clean_process_multiplier_ops",
    "shipping_flat_hours":           "runtime_calc/shipping_flat_hours",
    "shipping_boards_per_hour":      "runtime_calc/shipping_boards_per_hour",
}

def _coerce_nonnegative_float(raw: Any, default: float) -> float:
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        logger.warning("runtime_calc setting %r not a float; using default %s", raw, default)
        return default
    if not math.isfinite(parsed) or parsed < 0:
        logger.warning("runtime_calc setting %s out of range; using default %s", parsed, default)
        return default
    return parsed

class RuntimeCalcSettingsController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings) -> None:
        super().__init__()
        self._settings = settings

    def constants(self) -> RuntimeConstants:
        base = RuntimeConstants.defaults()
        resolved = {}
        for field_name, key in _RUNTIME_KEYS.items():
            try:
                raw_value = self._settings.value(key)
            except Exception:
                logger.warning("runtime_calc setting %s unreadable; using default", key, exc_info=True)
                raw_value = None
            resolved[field_name] = _coerce_nonnegative_float(raw_value, getattr(base, field_name))
        return RuntimeConstants(**resolved)

    def set_value(self, field_name: str, value: float) -> None:
        key = _RUNTIME_KEYS[field_name]
        default = getattr(RuntimeConstants.defaults(), field_name)
        if not (isinstance(value, (int, float)) and math.isfinite(value) and value >= 0):
            return
        try:
            stored_raw = self._settings.value(key)
        except Exception:
            stored_raw = None
        current = _coerce_nonnegative_float(stored_raw, default)
        if float(value) == current:
            return
        self._settings.setValue(key, float(value))
        self.changed.emit()

