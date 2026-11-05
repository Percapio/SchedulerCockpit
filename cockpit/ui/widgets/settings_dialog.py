"""Settings dialog (Phase 32, 2.4 + 3.2; Phase 54 §10.1–10.2)."""

import datetime
import logging
import pathlib
import sqlite3
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable

from PyQt6.QtCore import pyqtSignal, Qt, QObject, QThread, QSettings
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QComboBox, QFontComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QPushButton, QLabel, QFileDialog, QMessageBox, QWidget,
    QAbstractSpinBox, QLineEdit, QPlainTextEdit, QCheckBox, QScrollArea, QFrame,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView
)
from PyQt6.QtGui import QFont

from cockpit.ui import facelift
from cockpit.ui.ui_prefs import StyleController, export_diagnostics, RuntimeCalcSettingsController
from cockpit.ui.font_scale_controller import FontScaleController
from cockpit.services.mpn_library.credential_probe import (
    PROBE_RESULT_TEXT,
    CredentialProbeResult,
    probe_digikey_credentials,
)
from cockpit.services.requirements import (
    Failure,
    Result,
    Success,
    DEFAULT_VOCABULARY,
    DEFAULT_VOCABULARY_GENERATION,
    MAX_PATTERNS_PER_KIND,
    NothingFollows,
    NormalisedToken,
    PlaceholderFollows,
    RequirementEvidence,
    RequirementKind,
    RequirementPattern,
    RequirementScan,
    RequirementState,
    RequirementVocabulary,
    SectionAbsent,
    TableRows,
    UnitSource,
    Unrecognised,
    XraySection,
    export_vocabulary_file,
    import_vocabulary_file,
    load_vocabulary,
    parse_negation_line,
    parse_pattern_line,
    read_eco_units,
    render_negation_line,
    render_pattern_line,
    save_vocabulary_to_settings,
    scan_documents,
)
from cockpit.services.requirements_reconciler import _build_scan_inputs_for_audit

from cockpit.ui.widgets.qt_lifecycle import (
    FETCH_WATCHDOG_TIMEOUT_MS,
    _IN_FLIGHT_PROBES,
    start_bounded_probe,
)

logger = logging.getLogger(__name__)

_PRESET_LABELS = {facelift.DARK: "Dark", facelift.LIGHT: "Light"}

# Longer than the gateway's CONNECT_TIMEOUT_S so an ordinary connect timeout
# reports itself rather than being pre-empted by the watchdog. The watchdog is
# there for the proxy that accepts the connection and never answers.
#
# Distinct from FETCH_WATCHDOG_TIMEOUT_MS on purpose: that one is sized against
# an SMB directory scan, this one against an HTTPS connect. One constant per
# kind of wait.
PROBE_WATCHDOG_TIMEOUT_MS = 25_000

# _IN_FLIGHT_PROBES is re-exported from qt_lifecycle, where the seam that owns
# it lives. Bound here because this is the path callers and tests know it by.
__all_probe_registry__ = _IN_FLIGHT_PROBES

# Anchors background trial-scan threads past the TryProcessRequirementsDialog's
# lifetime (mirrors _READS_IN_FLIGHT in second_ops_dialog.py, Phase 54 §10.2).
_TRY_READS_IN_FLIGHT: set[tuple[QThread, QObject]] = set()


@dataclass(frozen=True)
class FieldSpec:
    label: str
    min: float
    max: float
    step: float
    decimals: int
    suffix: str


_RUNTIME_FIELD_SPECS: tuple[tuple[str, FieldSpec], ...] = (
    ("smt_placement_time_min",       FieldSpec("SMT placement time",             0.001,  1.0,    0.001,  3, " min")),
    ("tht_placement_time_min",       FieldSpec("THT placement time",             0.01,   5.0,    0.01,   2, " min")),
    ("aoi_inspection_time_min",      FieldSpec("AOI inspection time",            0.0001, 0.01,   0.0001, 4, " min")),
    ("class_3_multiplier_aoi",       FieldSpec("Class 3 multiplier (AOI)",       1.0,    3.0,    0.05,   2, "×")),
    ("class_3_multiplier_tht",       FieldSpec("Class 3 multiplier (THT)",       1.0,    3.0,    0.05,   2, "×")),
    ("clean_process_multiplier_tht", FieldSpec("Clean-process multiplier (THT)", 1.0,    3.0,    0.05,   2, "×")),
    ("clean_process_multiplier_ops", FieldSpec("Clean-process multiplier (OPS)", 1.0,    3.0,    0.05,   2, "×")),
    ("shipping_flat_hours",          FieldSpec("Shipping setup (flat)",          0.0,    40.0,   0.5,    1, " hr")),
    ("shipping_boards_per_hour",     FieldSpec("Shipping rate",                  1.0,    1000.0, 10.0,   0, "/hr")),
)


_REQUIREMENT_EDITOR_SPECS: tuple[tuple[RequirementKind, str], ...] = (
    (RequirementKind.XRAY_SAVE_IMAGES, "X-ray & save images"),
    (RequirementKind.XRAY, "X-ray (XR)"),
    (RequirementKind.PRESS_FIT, "Press fit (PF)"),
    (RequirementKind.AQUANOX, "Aquanox (AQ)"),
)

_UNIT_SOURCE_SPECS: tuple[tuple[UnitSource, str], ...] = (
    (UnitSource.ECO_TEXT, "ECO"),
    (UnitSource.BOM_NOTE, "BOM note"),
    (UnitSource.BOM_DESCRIPTION, "BOM description"),
    (UnitSource.TRAVELER_WASH, "Traveler wash"),
)


def _format_xray_section_reading(section: XraySection | None, error_reason: str | None = None) -> str:
    if error_reason:
        return f"X-ray section: could not read ECO ({error_reason})"
    if section is None:
        return "X-ray section: not read"
    if isinstance(section, TableRows):
        return f"X-ray section: table with {section.live_rows} live row(s)"
    if isinstance(section, PlaceholderFollows):
        return "X-ray section: marker followed by placeholder (e.g. N/A)"
    if isinstance(section, NothingFollows):
        return "X-ray section: marker followed by nothing"
    if isinstance(section, SectionAbsent):
        return "X-ray section: marker absent"
    if isinstance(section, Unrecognised):
        return f"X-ray section: unrecognised — followed by: {section.following}"
    return "X-ray section: unknown"


class _TrialScanWorker(QObject):
    """Runs scan_documents + read_eco_units off the UI thread with unsaved vocabulary (§10.2)."""

    trial_ready = pyqtSignal(object, object, object)  # RequirementScan, XraySection | None, str | None
    trial_failed = pyqtSignal(str)

    def __init__(self, inputs) -> None:
        super().__init__()
        self._inputs = inputs

    def run(self) -> None:
        try:
            scan = scan_documents(self._inputs)
            eco_res = read_eco_units(self._inputs.notes_path, self._inputs.vocabulary)
            if eco_res.is_ok:
                xray_sec = eco_res.value.xray_section
                err_reason = None
            else:
                xray_sec = None
                err_reason = eco_res.error.reason
            self.trial_ready.emit(scan, xray_sec, err_reason)
        except Exception as exc:
            self.trial_failed.emit(str(exc))


class TryProcessRequirementsDialog(QDialog):
    """Modal trial-run dialog for testing unsaved Process Requirements patterns on an open audit (§10.2)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        vocabulary: RequirementVocabulary,
        default_audit_id: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Try Process Requirements on an Audit")
        self.setModal(True)
        self.resize(760, 520)

        self._conn = conn
        self._vocabulary = vocabulary
        self._worker: _TrialScanWorker | None = None
        self._thread: QThread | None = None
        self._last_scan: RequirementScan | None = None
        self._last_xray_section: XraySection | None = None

        layout = QVBoxLayout(self)

        audits = self._load_open_audits()
        if not audits:
            lbl = QLabel("No open audits are available to test against.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl, stretch=1)
            self._add_close_row(layout)
            return

        picker_row = QHBoxLayout()
        picker_row.addWidget(QLabel("Audit:"))
        self.audit_combo = QComboBox()
        selected_idx = 0
        for idx, (audit_id, label) in enumerate(audits):
            self.audit_combo.addItem(label, audit_id)
            if default_audit_id is not None and audit_id == default_audit_id:
                selected_idx = idx
        self.audit_combo.setCurrentIndex(selected_idx)
        self.audit_combo.currentIndexChanged.connect(self._on_audit_changed)
        picker_row.addWidget(self.audit_combo, stretch=1)
        layout.addLayout(picker_row)

        self.status_label = QLabel("Scanning…")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.states_table = QTableWidget(len(_REQUIREMENT_EDITOR_SPECS), 3)
        self.states_table.setHorizontalHeaderLabels(["Requirement", "State", "Detail"])
        self.states_table.verticalHeader().setVisible(False)
        self.states_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.states_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.states_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.states_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.states_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.states_table.setMaximumHeight(160)
        layout.addWidget(self.states_table)

        self.xray_section_label = QLabel("")
        self.xray_section_label.setWordWrap(True)
        layout.addWidget(self.xray_section_label)

        ev_header = QLabel("Evidence (matched and negated):")
        layout.addWidget(ev_header)

        self.evidence_table = QTableWidget(0, 5)
        self.evidence_table.setHorizontalHeaderLabels(
            ["Requirement", "Status", "Source", "Location", "Excerpt"]
        )
        self.evidence_table.verticalHeader().setVisible(False)
        self.evidence_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.evidence_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.evidence_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.evidence_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.evidence_table, stretch=1)

        self._add_close_row(layout)
        self._start_scan_for_current_audit()

    def _add_close_row(self, layout: QVBoxLayout) -> None:
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_row.addWidget(self.close_btn)
        layout.addLayout(btn_row)

    def _load_open_audits(self) -> list[tuple[int, str]]:
        cur = self._conn.cursor()
        cur.execute(
            """
            SELECT id, part_number, work_order_ref, split_suffix
            FROM active_audits
            ORDER BY id ASC
            """
        )
        out: list[tuple[int, str]] = []
        for row in cur.fetchall():
            aid = int(row["id"])
            pn = str(row["part_number"])
            wo = str(row["work_order_ref"])
            suf = str(row["split_suffix"] or "")
            wo_disp = f"{wo}{suf}" if suf else wo
            out.append((aid, f"{pn} (WO: {wo_disp})"))
        return out

    def _on_audit_changed(self, _index: int) -> None:
        self._start_scan_for_current_audit()

    def _start_scan_for_current_audit(self) -> None:
        self._detach_worker()
        audit_id = self.audit_combo.currentData()
        if audit_id is None:
            return

        cur = self._conn.cursor()
        cur.execute(
            "SELECT traveler_metadata FROM active_audits WHERE id = ?",
            (int(audit_id),),
        )
        row = cur.fetchone()
        if not row:
            self.status_label.setText("Audit no longer exists.")
            return

        inputs = _build_scan_inputs_for_audit(
            self._conn, int(audit_id), row["traveler_metadata"], self._vocabulary
        )
        self.status_label.setText("Scanning documents with current editor patterns…")

        thread = QThread()
        worker = _TrialScanWorker(inputs)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.trial_ready.connect(self._on_trial_ready)
        worker.trial_failed.connect(self._on_trial_failed)

        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        pair = (thread, worker)
        _TRY_READS_IN_FLIGHT.add(pair)
        thread.finished.connect(lambda p=pair: _TRY_READS_IN_FLIGHT.discard(p))

        self._thread = thread
        self._worker = worker
        thread.start()

    def is_read_in_flight(self) -> bool:
        return self._worker is not None

    def _detach_worker(self) -> None:
        worker, thread = self._worker, self._thread
        self._worker = None
        self._thread = None
        if worker is not None:
            for sig in (worker.trial_ready, worker.trial_failed):
                try:
                    sig.disconnect()
                except (TypeError, RuntimeError):
                    pass
        if thread is not None:
            try:
                thread.quit()
            except RuntimeError:
                pass

    def reject(self) -> None:
        self._detach_worker()
        super().reject()

    def done(self, result_code: int) -> None:
        self._detach_worker()
        super().done(result_code)

    def _on_trial_failed(self, err: str) -> None:
        self._detach_worker()
        self.status_label.setText(f"Trial scan failed: {err}")

    def _on_trial_ready(
        self,
        scan: RequirementScan,
        xray_section: XraySection | None,
        eco_err: str | None,
    ) -> None:
        self._detach_worker()
        self._last_scan = scan
        self._last_xray_section = xray_section
        self.status_label.setText("Trial complete (unsaved — nothing written to database).")

        kind_labels = dict(_REQUIREMENT_EDITOR_SPECS)
        for row_idx, (kind, label) in enumerate(_REQUIREMENT_EDITOR_SPECS):
            st = scan.states.get(kind, RequirementState.UNKNOWN)
            detail = scan.unknown_reasons.get(kind, "")
            if kind == RequirementKind.XRAY_SAVE_IMAGES and scan.xray_task and not detail:
                detail = f"Task fingerprint: {scan.xray_task[:12]}…"
            self._set_plain_item(self.states_table, row_idx, 0, label)
            self._set_plain_item(self.states_table, row_idx, 1, st.value)
            self._set_plain_item(self.states_table, row_idx, 2, detail or "")

        self.xray_section_label.setText(_format_xray_section_reading(xray_section, eco_err))

        self.evidence_table.setRowCount(len(scan.evidence))
        for row_idx, ev in enumerate(scan.evidence):
            req_lbl = kind_labels.get(ev.kind, ev.kind.value)
            status_txt = "Negated" if ev.is_negated else "Matched"
            self._set_plain_item(self.evidence_table, row_idx, 0, req_lbl)
            self._set_plain_item(self.evidence_table, row_idx, 1, status_txt)
            self._set_plain_item(self.evidence_table, row_idx, 2, ev.source.value)
            self._set_plain_item(self.evidence_table, row_idx, 3, ev.location)
            self._set_plain_item(self.evidence_table, row_idx, 4, ev.excerpt)

    @staticmethod
    def _set_plain_item(table: QTableWidget, row: int, col: int, text: str) -> None:
        item = QTableWidgetItem(str(text))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        table.setItem(row, col, item)


class SettingsDialog(QDialog):
    """Display preferences (theme preset, font family, font size) + diagnostics export.

    Changes apply live and persist via the controllers' QSettings backing.
    Process Requirements apply when the operator clicks Save (§10.1).
    """

    reset_requested = pyqtSignal()
    requirements_vocabulary_saved = pyqtSignal(object)  # RequirementVocabulary

    def __init__(
        self,
        style_controller: StyleController,
        font_scale_controller: FontScaleController,
        runtime_settings_controller: RuntimeCalcSettingsController | None,
        second_ops_settings_controller,  # from cockpit.services.second_ops import SecondOpsSettingsController
        config,
        parent: QWidget | None = None,
        source_root_controller=None,
        mpn_library_controller=None,
        enrichment_in_flight=None,
        *,
        settings: QSettings | None = None,
        conn: sqlite3.Connection | None = None,
        current_audit_id: int | None = None,
        initial_vocabulary: RequirementVocabulary | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(680, 720)
        self._style = style_controller
        self._font_scale = font_scale_controller
        self._config = config
        self._settings = settings
        self._conn = conn
        self._current_audit_id = current_audit_id
        self.terms_edit: QLineEdit | None = None
        self._second_ops_controller = None

        self._mpn_controller = None
        self._client_id_edit: QLineEdit | None = None
        self._client_secret_edit: QLineEdit | None = None
        self._api_base_edit: QLineEdit | None = None
        self._mpn_status_lbl: QLabel | None = None
        self._test_connection_btn: QPushButton | None = None
        self._forget_credentials_btn: QPushButton | None = None
        self._plaintext_disclosure_lbl: QLabel | None = None
        self._credential_block: QWidget | None = None
        self._probe_in_flight = False
        self._probe_admission = None
        self._source_root_admission = None
        # Supplied by MainWindow, which is the only object that can see a
        # running enrichment. Absent in tests that build the dialog alone.
        self._enrichment_in_flight = enrichment_in_flight or (lambda: False)

        # Process Requirements editor state (§10.1, §10.2)
        if initial_vocabulary is not None:
            loaded_vocab = initial_vocabulary
        elif self._settings is not None:
            loaded_vocab = load_vocabulary(self._settings).vocabulary
        else:
            loaded_vocab = DEFAULT_VOCABULARY
        self._saved_vocabulary: RequirementVocabulary = loaded_vocab
        self._editor_generation: int = loaded_vocab.generation
        self._loading_vocabulary_widgets: bool = False
        self.req_pattern_edits: dict[RequirementKind, QPlainTextEdit] = {}
        self.req_source_checkboxes: dict[RequirementKind, dict[UnitSource, QCheckBox]] = {}
        self.req_restore_buttons: dict[RequirementKind, QPushButton] = {}
        self.section_markers_edit: QPlainTextEdit | None = None
        self.section_markers_restore_btn: QPushButton | None = None
        self.negations_before_edit: QPlainTextEdit | None = None
        self.negations_before_restore_btn: QPushButton | None = None
        self.negations_after_edit: QPlainTextEdit | None = None
        self.negations_after_restore_btn: QPushButton | None = None
        self.req_window_hint_lbl: QLabel | None = None
        self.req_validation_lbl: QLabel | None = None
        self.req_import_summary_lbl: QLabel | None = None
        self.req_save_btn: QPushButton | None = None
        self.req_restore_all_btn: QPushButton | None = None
        self.req_try_btn: QPushButton | None = None
        self.req_export_btn: QPushButton | None = None
        self.req_import_btn: QPushButton | None = None

        outer_layout = QVBoxLayout(self)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        content_widget = QWidget()
        layout = QVBoxLayout(content_widget)
        self.scroll_area.setWidget(content_widget)
        outer_layout.addWidget(self.scroll_area, stretch=1)

        form = QFormLayout()

        self.theme_combo = QComboBox()
        for preset in facelift.PRESETS:
            self.theme_combo.addItem(_PRESET_LABELS[preset], preset)
        idx = self.theme_combo.findData(self._style.preset())
        if idx >= 0:
            self.theme_combo.setCurrentIndex(idx)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("Theme:", self.theme_combo)

        self.font_combo = QFontComboBox()
        current_family = self._style.font_family()
        if current_family:
            self.font_combo.setCurrentFont(QFont(current_family))
        self.font_combo.currentFontChanged.connect(self._on_font_changed)
        form.addRow("Font:", self.font_combo)

        bounds = self._font_scale._bounds
        self.size_spin = QSpinBox()
        self.size_spin.setRange(bounds.min_pt, bounds.max_pt)
        self.size_spin.setSingleStep(bounds.step_pt)
        self.size_spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.size_spin.setKeyboardTracking(False)
        self.size_spin.setValue(self._font_scale.current_pt())
        self.size_spin.setSuffix(" pt")
        self.size_spin.valueChanged.connect(self._on_size_changed)
        self._font_scale.scale_changed.connect(self._sync_spin_from_controller)
        form.addRow("Font size:", self.size_spin)

        layout.addLayout(form)
        layout.addSpacing(12)

        if runtime_settings_controller is not None:
            calc_group = self._build_calculation_settings(runtime_settings_controller)
            layout.addWidget(calc_group)
            layout.addSpacing(12)

        if second_ops_settings_controller is not None:
            second_ops_group = self._build_second_ops_settings(second_ops_settings_controller)
            layout.addWidget(second_ops_group)
            layout.addSpacing(12)

        process_req_group = self._build_process_requirements_settings(loaded_vocab)
        layout.addWidget(process_req_group)
        layout.addSpacing(12)

        if source_root_controller is not None:
            source_root_group = self._build_source_root_settings(source_root_controller)
            layout.addWidget(source_root_group)
            layout.addSpacing(12)

        if mpn_library_controller is not None:
            mpn_library_group = self._build_mpn_library_settings(mpn_library_controller)
            layout.addWidget(mpn_library_group)
            layout.addSpacing(12)

        reset_group = QGroupBox("Application Data")
        reset_layout = QVBoxLayout(reset_group)
        reset_label = QLabel("Clear all audit data. Holidays, schema version, and logs are retained.")
        reset_label.setWordWrap(True)
        reset_layout.addWidget(reset_label)
        reset_btn = QPushButton("Reset application data...")
        reset_btn.clicked.connect(self.reset_requested.emit)
        reset_layout.addWidget(reset_btn)
        layout.addWidget(reset_group)
        layout.addSpacing(12)

        diag_label = QLabel("Trouble? Package the logs for the development team:")
        diag_label.setWordWrap(True)
        layout.addWidget(diag_label)

        export_btn = QPushButton("Export Diagnostics/Logs...")
        export_btn.clicked.connect(self._on_export_diagnostics)
        layout.addWidget(export_btn)

        layout.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        outer_layout.addLayout(buttons)

    # --- live-apply handlers ---

    def _on_theme_changed(self, _index: int) -> None:
        self._style.set_preset(self.theme_combo.currentData())

    def _on_font_changed(self, font: QFont) -> None:
        self._style.set_font_family(font.family())

    def _on_size_changed(self, pt: int) -> None:
        self._font_scale.set_pt(pt)
        effective: int = self._font_scale.current_pt()
        if effective != pt:
            self._sync_spin_from_controller(effective)

    def _sync_spin_from_controller(self, pt: int) -> None:
        if self.size_spin.value() == pt:
            return
        self.size_spin.blockSignals(True)
        self.size_spin.setValue(pt)
        self.size_spin.blockSignals(False)

    def _on_export_diagnostics(self) -> None:
        stamp = datetime.date.today().isoformat()
        suggested = str(pathlib.Path.home() / f"cockpit-diagnostics-{stamp}.zip")
        target, _ = QFileDialog.getSaveFileName(
            self, "Export Diagnostics", suggested, "Zip archives (*.zip)"
        )
        if not target:
            return
        try:
            archived = export_diagnostics(self._config, pathlib.Path(target))
        except Exception as e:
            logger.exception("Diagnostics export failed")
            QMessageBox.critical(self, "Export failed", f"Could not write the archive:\n{e}")
            return
        QMessageBox.information(
            self, "Diagnostics exported",
            f"Packaged {len(archived)} file(s) into:\n{target}"
        )

    def _build_calculation_settings(self, controller: RuntimeCalcSettingsController) -> QGroupBox:
        current_constants = controller.constants()
        group = QGroupBox("Calculation Settings")
        form = QFormLayout(group)
        for field_name, spec in _RUNTIME_FIELD_SPECS:
            spin = QDoubleSpinBox()
            spin.setRange(spec.min, spec.max)
            spin.setSingleStep(spec.step)
            spin.setDecimals(spec.decimals)
            spin.setSuffix(spec.suffix)
            spin.valueChanged.connect(partial(controller.set_value, field_name))
            spin.blockSignals(True)
            spin.setValue(getattr(current_constants, field_name))
            spin.blockSignals(False)
            if field_name == "clean_process_multiplier_ops":
                spin.setToolTip("Applies once OPS is computed (a later phase)")
            form.addRow(spec.label + ":", spin)
        return group

    def _build_second_ops_settings(self, controller) -> QGroupBox:
        group = QGroupBox("2nd OPS")
        layout = QVBoxLayout(group)

        self._second_ops_controller = controller
        self.terms_edit = QLineEdit()
        self.terms_edit.setText(", ".join(controller.terms()))
        self.terms_edit.editingFinished.connect(lambda: controller.set_terms_from_text(self.terms_edit.text()))
        layout.addWidget(self.terms_edit)

        restore_btn = QPushButton("Restore defaults")
        restore_btn.clicked.connect(controller.restore_defaults)

        controller.changed.connect(self._update_edit)

        layout.addWidget(restore_btn)

        info = QLabel("Matched against part number and description, whole words only, case-insensitive")
        info.setWordWrap(True)
        layout.addWidget(info)

        return group

    # --- Process Requirements (§10.1, §10.2) -----------------------------

    def _build_process_requirements_settings(self, initial_vocab: RequirementVocabulary) -> QGroupBox:
        group = QGroupBox("Process Requirements")
        layout = QVBoxLayout(group)

        for kind, label in _REQUIREMENT_EDITOR_SPECS:
            header_row = QHBoxLayout()
            header_row.addWidget(QLabel(f"{label}:"))
            header_row.addStretch()
            restore_box_btn = QPushButton("Restore defaults")
            # A bound method, not functools.partial(self._restore..., kind):
            # PyQt holds a partial strongly, which ties this dialog's wrapper
            # into a reference cycle that outlives WA_DeleteOnClose. The next
            # application restyle then dereferenced the destroyed dialog and
            # crashed the process. Bound methods of a QObject are held weakly.
            restore_box_btn.setProperty("requirement_kind", kind.value)
            restore_box_btn.clicked.connect(self._on_restore_requirement_box_clicked)
            self.req_restore_buttons[kind] = restore_box_btn
            header_row.addWidget(restore_box_btn)
            layout.addLayout(header_row)

            edit = QPlainTextEdit()
            edit.setMaximumHeight(72)
            edit.textChanged.connect(self._on_process_requirements_edited)
            self.req_pattern_edits[kind] = edit
            layout.addWidget(edit)

            sources_row = QHBoxLayout()
            sources_row.addWidget(QLabel("Sources:"))
            cb_map: dict[UnitSource, QCheckBox] = {}
            for src, src_label in _UNIT_SOURCE_SPECS:
                cb = QCheckBox(src_label)
                cb.toggled.connect(self._on_process_requirements_edited)
                cb_map[src] = cb
                sources_row.addWidget(cb)
            sources_row.addStretch()
            self.req_source_checkboxes[kind] = cb_map
            layout.addLayout(sources_row)

        # X-ray section markers box
        markers_header = QHBoxLayout()
        markers_header.addWidget(QLabel("X-ray section markers:"))
        markers_header.addStretch()
        self.section_markers_restore_btn = QPushButton("Restore defaults")
        self.section_markers_restore_btn.clicked.connect(self._restore_section_markers_defaults)
        markers_header.addWidget(self.section_markers_restore_btn)
        layout.addLayout(markers_header)

        self.section_markers_edit = QPlainTextEdit()
        self.section_markers_edit.setMaximumHeight(60)
        self.section_markers_edit.textChanged.connect(self._on_process_requirements_edited)
        layout.addWidget(self.section_markers_edit)

        # Negations before match
        neg_before_header = QHBoxLayout()
        neg_before_header.addWidget(QLabel("Negations before match:"))
        neg_before_header.addStretch()
        self.negations_before_restore_btn = QPushButton("Restore defaults")
        self.negations_before_restore_btn.clicked.connect(self._restore_negations_before_defaults)
        neg_before_header.addWidget(self.negations_before_restore_btn)
        layout.addLayout(neg_before_header)

        self.negations_before_edit = QPlainTextEdit()
        self.negations_before_edit.setMaximumHeight(60)
        self.negations_before_edit.textChanged.connect(self._on_process_requirements_edited)
        layout.addWidget(self.negations_before_edit)

        # Negations after match
        neg_after_header = QHBoxLayout()
        neg_after_header.addWidget(QLabel("Negations after match:"))
        neg_after_header.addStretch()
        self.negations_after_restore_btn = QPushButton("Restore defaults")
        self.negations_after_restore_btn.clicked.connect(self._restore_negations_after_defaults)
        neg_after_header.addWidget(self.negations_after_restore_btn)
        layout.addLayout(neg_after_header)

        self.negations_after_edit = QPlainTextEdit()
        self.negations_after_edit.setMaximumHeight(60)
        self.negations_after_edit.textChanged.connect(self._on_process_requirements_edited)
        layout.addWidget(self.negations_after_edit)

        self.req_window_hint_lbl = QLabel(
            "`~ N`: every part within N words of the others. `~ 1` means next to each other."
        )
        self.req_window_hint_lbl.setWordWrap(True)
        layout.addWidget(self.req_window_hint_lbl)

        self.req_validation_lbl = QLabel("")
        self.req_validation_lbl.setWordWrap(True)
        self.req_validation_lbl.setStyleSheet("color: #d9534f; font-weight: bold;")
        self.req_validation_lbl.hide()
        layout.addWidget(self.req_validation_lbl)

        self.req_import_summary_lbl = QLabel("")
        self.req_import_summary_lbl.setWordWrap(True)
        self.req_import_summary_lbl.hide()
        layout.addWidget(self.req_import_summary_lbl)

        action_row = QHBoxLayout()
        self.req_save_btn = QPushButton("Save")
        self.req_save_btn.clicked.connect(self._on_save_process_requirements)
        action_row.addWidget(self.req_save_btn)

        self.req_restore_all_btn = QPushButton("Restore defaults")
        self.req_restore_all_btn.clicked.connect(self._restore_all_process_requirements_defaults)
        action_row.addWidget(self.req_restore_all_btn)

        self.req_try_btn = QPushButton("Try on an audit…")
        self.req_try_btn.clicked.connect(self._on_try_process_requirements)
        action_row.addWidget(self.req_try_btn)

        self.req_export_btn = QPushButton("Export…")
        self.req_export_btn.clicked.connect(self._on_export_process_requirements)
        action_row.addWidget(self.req_export_btn)

        self.req_import_btn = QPushButton("Import…")
        self.req_import_btn.clicked.connect(self._on_import_process_requirements)
        action_row.addWidget(self.req_import_btn)

        action_row.addStretch()
        layout.addLayout(action_row)

        self._populate_editor_from_vocabulary(initial_vocab)
        return group

    def _populate_editor_from_vocabulary(self, vocab: RequirementVocabulary) -> None:
        self._loading_vocabulary_widgets = True
        try:
            self._editor_generation = vocab.generation
            for kind, _label in _REQUIREMENT_EDITOR_SPECS:
                patterns = vocab.patterns.get(kind, ())
                text = "\n".join(render_pattern_line(p) for p in patterns)
                self.req_pattern_edits[kind].setPlainText(text)
                admitted = vocab.admitted_sources.get(kind, frozenset())
                for src, cb in self.req_source_checkboxes[kind].items():
                    cb.setChecked(src in admitted)

            if self.section_markers_edit is not None:
                self.section_markers_edit.setPlainText(
                    "\n".join(render_pattern_line(p) for p in vocab.xray_section_markers)
                )
            if self.negations_before_edit is not None:
                self.negations_before_edit.setPlainText(
                    "\n".join(render_negation_line(p) for p in vocab.negations_before)
                )
            if self.negations_after_edit is not None:
                self.negations_after_edit.setPlainText(
                    "\n".join(render_negation_line(p) for p in vocab.negations_after)
                )
        finally:
            self._loading_vocabulary_widgets = False
        self._validate_process_requirements_editor()

    def _on_restore_requirement_box_clicked(self) -> None:
        button = self.sender()
        kind_value = button.property("requirement_kind") if button is not None else None
        if kind_value:
            self._restore_requirement_box_defaults(RequirementKind(kind_value))

    def _restore_requirement_box_defaults(self, kind: RequirementKind) -> None:
        self._loading_vocabulary_widgets = True
        try:
            patterns = DEFAULT_VOCABULARY.patterns.get(kind, ())
            self.req_pattern_edits[kind].setPlainText(
                "\n".join(render_pattern_line(p) for p in patterns)
            )
            admitted = DEFAULT_VOCABULARY.admitted_sources.get(kind, frozenset())
            for src, cb in self.req_source_checkboxes[kind].items():
                cb.setChecked(src in admitted)
        finally:
            self._loading_vocabulary_widgets = False
        self._validate_process_requirements_editor()

    def _restore_section_markers_defaults(self) -> None:
        if self.section_markers_edit is None:
            return
        self.section_markers_edit.setPlainText(
            "\n".join(render_pattern_line(p) for p in DEFAULT_VOCABULARY.xray_section_markers)
        )

    def _restore_negations_before_defaults(self) -> None:
        if self.negations_before_edit is None:
            return
        self.negations_before_edit.setPlainText(
            "\n".join(render_negation_line(p) for p in DEFAULT_VOCABULARY.negations_before)
        )

    def _restore_negations_after_defaults(self) -> None:
        if self.negations_after_edit is None:
            return
        self.negations_after_edit.setPlainText(
            "\n".join(render_negation_line(p) for p in DEFAULT_VOCABULARY.negations_after)
        )

    def _restore_all_process_requirements_defaults(self) -> None:
        if self.req_import_summary_lbl is not None:
            self.req_import_summary_lbl.clear()
            self.req_import_summary_lbl.hide()
        self._populate_editor_from_vocabulary(DEFAULT_VOCABULARY)

    def _on_process_requirements_edited(self) -> None:
        if self._loading_vocabulary_widgets:
            return
        self._validate_process_requirements_editor()

    def current_editor_vocabulary(self) -> Result[RequirementVocabulary, str]:
        """Parses all 7 boxes and source checkboxes into a RequirementVocabulary or returns the first error."""
        parsed_patterns: dict[RequirementKind, tuple[RequirementPattern, ...]] = {}
        parsed_sources: dict[RequirementKind, frozenset[UnitSource]] = {}

        for kind, label in _REQUIREMENT_EDITOR_SPECS:
            edit = self.req_pattern_edits[kind]
            pats: list[RequirementPattern] = []
            for line_no, raw_line in enumerate(edit.toPlainText().splitlines(), 1):
                if not raw_line.strip():
                    continue
                res = parse_pattern_line(raw_line)
                if res.is_err:
                    err = res.error
                    return Failure(
                        f"{label} (line {line_no}, pos {err.position}): {err.reason}"
                    )
                pats.append(res.value)
            if len(pats) > MAX_PATTERNS_PER_KIND:
                return Failure(
                    f"{label}: exceeds maximum of {MAX_PATTERNS_PER_KIND} patterns ({len(pats)})"
                )
            parsed_patterns[kind] = tuple(pats)

            admitted = frozenset(
                src for src, cb in self.req_source_checkboxes[kind].items() if cb.isChecked()
            )
            parsed_sources[kind] = admitted

        markers: list[RequirementPattern] = []
        if self.section_markers_edit is not None:
            for line_no, raw_line in enumerate(self.section_markers_edit.toPlainText().splitlines(), 1):
                if not raw_line.strip():
                    continue
                res = parse_pattern_line(raw_line)
                if res.is_err:
                    err = res.error
                    return Failure(
                        f"X-ray section markers (line {line_no}, pos {err.position}): {err.reason}"
                    )
                markers.append(res.value)
            if len(markers) > MAX_PATTERNS_PER_KIND:
                return Failure(
                    f"X-ray section markers: exceeds maximum of {MAX_PATTERNS_PER_KIND} patterns ({len(markers)})"
                )

        def _parse_neg_box(edit: QPlainTextEdit | None, box_label: str) -> Result[tuple[tuple[NormalisedToken, ...], ...], str]:
            if edit is None:
                return Success(())
            phrases: list[tuple[NormalisedToken, ...]] = []
            for line_no, raw_line in enumerate(edit.toPlainText().splitlines(), 1):
                if not raw_line.strip():
                    continue
                res = parse_negation_line(raw_line)
                if res.is_err:
                    err = res.error
                    return Failure(
                        f"{box_label} (line {line_no}, pos {err.position}): {err.reason}"
                    )
                phrases.append(res.value)
            if len(phrases) > MAX_PATTERNS_PER_KIND:
                return Failure(
                    f"{box_label}: exceeds maximum of {MAX_PATTERNS_PER_KIND} phrases ({len(phrases)})"
                )
            return Success(tuple(phrases))

        neg_before_res = _parse_neg_box(self.negations_before_edit, "Negations before match")
        if neg_before_res.is_err:
            return Failure(neg_before_res.error)

        neg_after_res = _parse_neg_box(self.negations_after_edit, "Negations after match")
        if neg_after_res.is_err:
            return Failure(neg_after_res.error)

        return Success(
            RequirementVocabulary(
                patterns=parsed_patterns,
                xray_section_markers=tuple(markers),
                admitted_sources=parsed_sources,
                negations_before=neg_before_res.value,
                negations_after=neg_after_res.value,
                generation=self._editor_generation,
            )
        )

    def _validate_process_requirements_editor(self) -> bool:
        res = self.current_editor_vocabulary()
        valid = res.is_ok
        if self.req_save_btn is not None:
            self.req_save_btn.setEnabled(valid)
        if self.req_try_btn is not None:
            self.req_try_btn.setEnabled(valid)
        if self.req_export_btn is not None:
            self.req_export_btn.setEnabled(valid)
        if self.req_validation_lbl is not None:
            if valid:
                self.req_validation_lbl.clear()
                self.req_validation_lbl.hide()
            else:
                self.req_validation_lbl.setText(res.error)
                self.req_validation_lbl.show()
        return valid

    def _on_save_process_requirements(self) -> None:
        res = self.current_editor_vocabulary()
        if res.is_err:
            self._validate_process_requirements_editor()
            return
        vocab = res.value
        if self._settings is not None:
            save_vocabulary_to_settings(self._settings, vocab)
        self._saved_vocabulary = vocab
        if self.req_import_summary_lbl is not None:
            self.req_import_summary_lbl.clear()
            self.req_import_summary_lbl.hide()
        self.requirements_vocabulary_saved.emit(vocab)

    def _on_try_process_requirements(self) -> None:
        res = self.current_editor_vocabulary()
        if res.is_err:
            self._validate_process_requirements_editor()
            return
        if self._conn is None:
            QMessageBox.information(
                self,
                "Try on an Audit",
                "No database connection is available in this context.",
            )
            return
        dlg = TryProcessRequirementsDialog(
            self._conn,
            res.value,
            default_audit_id=self._current_audit_id,
            parent=self,
        )
        dlg.exec()

    def _on_export_process_requirements(self) -> None:
        res = self.current_editor_vocabulary()
        if res.is_err:
            self._validate_process_requirements_editor()
            return
        suggested = str(pathlib.Path.home() / "cockpit-process-requirements.json")
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export Process Requirements",
            suggested,
            "JSON files (*.json)",
        )
        if not target:
            return
        try:
            export_vocabulary_file(pathlib.Path(target), res.value)
        except Exception as exc:
            logger.exception("Failed to export process requirements vocabulary")
            QMessageBox.critical(
                self,
                "Export Failed",
                f"Could not write process requirements file:\n{exc}",
            )

    def _on_import_process_requirements(self) -> None:
        target, _ = QFileDialog.getOpenFileName(
            self,
            "Import Process Requirements",
            str(pathlib.Path.home()),
            "JSON files (*.json)",
        )
        if not target:
            return
        self.import_vocabulary_from_path(pathlib.Path(target))

    def import_vocabulary_from_path(self, path: pathlib.Path) -> Result[RequirementVocabulary, str]:
        """Reads and loads an exported vocabulary into the editor unsaved, showing a diff summary (§10.2)."""
        prior_res = self.current_editor_vocabulary()
        prior_vocab = prior_res.value if prior_res.is_ok else self._saved_vocabulary

        imp_res = import_vocabulary_file(path)
        if imp_res.is_err:
            QMessageBox.warning(
                self,
                "Import Refused",
                imp_res.error,
            )
            return imp_res

        imported = imp_res.value
        summary_text = self._summarise_vocabulary_diff(prior_vocab, imported)
        self._populate_editor_from_vocabulary(imported)
        if self.req_import_summary_lbl is not None:
            self.req_import_summary_lbl.setText(summary_text)
            self.req_import_summary_lbl.show()
        return Success(imported)

    @staticmethod
    def _summarise_vocabulary_diff(
        before: RequirementVocabulary,
        after: RequirementVocabulary,
    ) -> str:
        before_lines: set[str] = set()
        after_lines: set[str] = set()
        for kind, _ in _REQUIREMENT_EDITOR_SPECS:
            for p in before.patterns.get(kind, ()):
                before_lines.add(f"{kind.value}:{render_pattern_line(p)}")
            for p in after.patterns.get(kind, ()):
                after_lines.add(f"{kind.value}:{render_pattern_line(p)}")
        for p in before.xray_section_markers:
            before_lines.add(f"markers:{render_pattern_line(p)}")
        for p in after.xray_section_markers:
            after_lines.add(f"markers:{render_pattern_line(p)}")
        for p in before.negations_before:
            before_lines.add(f"neg_before:{render_negation_line(p)}")
        for p in after.negations_before:
            after_lines.add(f"neg_before:{render_negation_line(p)}")
        for p in before.negations_after:
            before_lines.add(f"neg_after:{render_negation_line(p)}")
        for p in after.negations_after:
            after_lines.add(f"neg_after:{render_negation_line(p)}")

        added = len(after_lines - before_lines)
        removed = len(before_lines - after_lines)
        return (
            f"Imported into editor (unsaved): {added} pattern(s) added, "
            f"{removed} removed (generation {after.generation}). Click Save to apply."
        )

    def _build_source_root_settings(self, controller) -> QGroupBox:
        from cockpit.settings.source_root import SourceRootState, probe_source_root, RootProbeResult

        group = QGroupBox("Source Root")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("Path:"))
        edit = QLineEdit()

        state, path = controller.source_root()
        if state == SourceRootState.CONFIGURED:
            edit.setText(str(path))
        elif state == SourceRootState.MALFORMED:
            edit.setText(str(path))

        row.addWidget(edit)
        layout.addLayout(row)

        status_lbl = QLabel("")
        layout.addWidget(status_lbl)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        validate_btn = QPushButton("Validate")
        btn_row.addWidget(validate_btn)
        layout.addLayout(btn_row)

        edit.textChanged.connect(controller.set_source_root)

        def on_validate():
            current_state, current_path = controller.source_root()
            if current_state != SourceRootState.CONFIGURED:
                status_lbl.setText("Error: Path must be absolute.")
                return

            validate_btn.setEnabled(False)
            status_lbl.setText("Checking...")

            def on_outcome(result: RootProbeResult) -> None:
                validate_btn.setEnabled(True)
                if result == RootProbeResult.REACHABLE:
                    status_lbl.setText("Reachable.")
                elif result == RootProbeResult.REACHABLE_BUT_NO_JOB_TREE:
                    status_lbl.setText("Reachable, but no job tree found.")
                else:
                    status_lbl.setText("Unreachable.")

            def on_timeout() -> None:
                # The share accepted nothing and answered nothing. Saying so
                # beats leaving Validate disabled on "Checking..." forever,
                # which is what happened before there was a watchdog.
                validate_btn.setEnabled(True)
                status_lbl.setText(
                    f"No answer from the share within "
                    f"{FETCH_WATCHDOG_TIMEOUT_MS // 1000} seconds."
                )

            def on_error(exc: BaseException) -> None:
                # probe_source_root catches OSError itself, so reaching here is
                # a defect rather than an unreachable share. Named as such.
                validate_btn.setEnabled(True)
                status_lbl.setText("Could not check the path; see the log.")

            self._source_root_admission = start_bounded_probe(
                owner=self,
                work=partial(probe_source_root, current_path),
                watchdog_ms=FETCH_WATCHDOG_TIMEOUT_MS,
                on_outcome=on_outcome,
                on_timeout=on_timeout,
                on_error=on_error,
            )

        validate_btn.clicked.connect(on_validate)
        return group

    def _build_mpn_library_settings(self, controller) -> QGroupBox:
        from cockpit.settings.mpn_library import (
            enrichment_call_ceiling_default,
            min_call_ceiling,
        )

        self._mpn_controller = controller
        group = QGroupBox("MPN Library (Alpha)")
        form = QFormLayout(group)

        enable_cb = QCheckBox("Enable MPN Library")
        enable_cb.setChecked(controller.is_enabled())
        enable_cb.stateChanged.connect(lambda state: controller.set_enabled(bool(state)))
        form.addRow(enable_cb)

        stale_spin = QSpinBox()
        stale_spin.setRange(1, 3650)
        stale_spin.setSuffix(" days")
        stale_spin.setValue(controller.stale_after_days())
        stale_spin.valueChanged.connect(controller.set_stale_after_days)
        form.addRow("Stale after:", stale_spin)

        # Everything below the divider lives in one container so the alpha gate
        # can hide it as a unit. Hiding a parent does not destroy children, so
        # the widget references below and the done() teardown are unaffected.
        self._credential_block = QWidget()
        block_form = QFormLayout(self._credential_block)
        block_form.setContentsMargins(0, 0, 0, 0)
        form.addRow(self._credential_block)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.HLine)
        block_form.addRow(divider)

        # Credentials commit on focus-out and on dialog accept, never per
        # keystroke: wiring textChanged straight through would flush every
        # intermediate keystroke to disk and leave the pair half-written for
        # as long as the operator is typing the second field.
        self._client_id_edit = QLineEdit()
        self._client_id_edit.setText(self._stored_client_id_text())
        self._client_id_edit.editingFinished.connect(self._commit_digikey_credentials)
        block_form.addRow("Client ID:", self._client_id_edit)

        # Password echo is a shoulder-surfing control. It is not storage
        # security, and the disclosure label below says so. The field is never
        # populated from storage: nobody needs to read a secret back, only to
        # replace it, and a populated widget puts it in the Qt object tree.
        self._client_secret_edit = QLineEdit()
        self._client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._client_secret_edit.setPlaceholderText(self._secret_placeholder_text())
        self._client_secret_edit.editingFinished.connect(self._commit_digikey_credentials)
        block_form.addRow("Client Secret:", self._client_secret_edit)

        self._api_base_edit = QLineEdit()
        self._api_base_edit.setText(controller.api_base_url_text())
        self._api_base_edit.editingFinished.connect(self._commit_api_base_url)
        block_form.addRow("API host:", self._api_base_edit)

        ceiling_spin = QSpinBox()
        ceiling_spin.setRange(min_call_ceiling(), 100000)
        ceiling_spin.setKeyboardTracking(False)
        ceiling_spin.setValue(controller.call_ceiling())
        ceiling_spin.setToolTip(
            "Total API calls one enrichment run may make, token acquisition "
            f"included. Default {enrichment_call_ceiling_default()}."
        )
        ceiling_spin.valueChanged.connect(controller.set_call_ceiling)
        block_form.addRow("Requests per run:", ceiling_spin)

        self._mpn_status_lbl = QLabel("")
        self._mpn_status_lbl.setWordWrap(True)
        block_form.addRow(self._mpn_status_lbl)

        btn_row = QHBoxLayout()
        self._test_connection_btn = QPushButton("Test Connection")
        self._test_connection_btn.clicked.connect(self._on_test_connection)
        btn_row.addWidget(self._test_connection_btn)
        btn_row.addStretch()
        self._forget_credentials_btn = QPushButton("Forget Credentials")
        self._forget_credentials_btn.clicked.connect(self._on_forget_credentials)
        btn_row.addWidget(self._forget_credentials_btn)
        block_form.addRow(btn_row)

        self._plaintext_disclosure_lbl = QLabel(
            "Credentials are stored unencrypted in settings.ini."
        )
        self._plaintext_disclosure_lbl.setWordWrap(True)
        block_form.addRow(self._plaintext_disclosure_lbl)

        enable_cb.toggled.connect(self.set_credential_block_visible)
        self.set_credential_block_visible(controller.is_enabled())
        self._refresh_mpn_credential_state()
        return group

    def set_credential_block_visible(self, visible: bool) -> None:
        """Shows or hides the credential controls behind the alpha gate.

        post: the container is visible iff the library is enabled; a probe in
              flight is torn down before the container is hidden, and the
              buttons and status label are re-rendered from the resulting
              state rather than left on their in-flight text
        """
        if self._credential_block is None:
            return
        if not visible and self._probe_in_flight:
            # Teardown alone clears the flag without re-rendering, which would
            # leave both buttons disabled and the label on "Checking..." with
            # no worker behind it.
            self._teardown_credential_probe()
            self._refresh_mpn_credential_state()
        self._credential_block.setVisible(bool(visible))


    # --- MPN library credentials -----------------------------------------

    def _stored_client_id_text(self) -> str:
        from cockpit.settings.mpn_library import Configured

        stored = self._mpn_controller.digikey_credentials()
        if isinstance(stored, Configured):
            return stored.credentials.client_id
        # A stored id with no secret still belongs in the field: it is not
        # secret, and leaving it blank would hide the half the operator has.
        return self._mpn_controller.stored_client_id()

    def _secret_placeholder_text(self) -> str:
        if self._mpn_controller.has_stored_secret():
            return "A secret is stored. Type to replace it."
        return "No secret stored."

    def _commit_digikey_credentials(self) -> None:
        if self._mpn_controller is None:
            return
        self._commit_credential_text(
            self._client_id_edit.text().strip(),
            self._client_secret_edit.text().strip(),
        )
        self._client_secret_edit.setPlaceholderText(self._secret_placeholder_text())
        self._refresh_mpn_credential_state()

    def _commit_credential_text(self, id_text: str, secret_text: str) -> None:
        """The Phase 48 section 4.3 commit table.

        Nothing an operator typed is destroyed by a commit that declines to
        fire, and no commit ever writes a half-populated pair: every path
        writes a complete pair, writes an id over an existing secret, or
        writes nothing at all. A blank secret field means "unchanged" when a
        secret is stored and "not yet entered" when one is not.
        """
        if not id_text:
            return
        if secret_text:
            self._mpn_controller.set_digikey_credentials(id_text, secret_text)
            return
        if self._mpn_controller.has_stored_secret():
            self._mpn_controller.set_digikey_client_id(id_text)

    def _commit_api_base_url(self) -> None:
        if self._mpn_controller is None:
            return
        self._mpn_controller.set_api_base_url(self._api_base_edit.text())
        self._refresh_mpn_credential_state()

    def _on_forget_credentials(self) -> None:
        reply = QMessageBox.question(
            self, "Forget Credentials",
            "Remove the stored DigiKey client ID and secret from settings.ini?\n\n"
            "The MPN Library stays enabled and your curated library is untouched.",
            QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel
        )
        if reply != QMessageBox.StandardButton.Ok:
            return
        self._mpn_controller.forget_digikey_credentials()
        self._client_id_edit.clear()
        self._client_secret_edit.clear()
        self._client_secret_edit.setPlaceholderText(self._secret_placeholder_text())
        self._refresh_mpn_credential_state()

    def _refresh_mpn_credential_state(self, status_override: str | None = None) -> None:
        """Renders the Phase 48 section 4.4 state table."""
        from cockpit.settings.mpn_library import (
            BASE_URL_FAULT_TEXT,
            Accepted,
            Configured,
            MissingCredentialField,
            Partial,
        )

        if self._mpn_controller is None:
            return

        # The two in-flight rows override everything below them. Forget is
        # disabled during a run because an operator who clicks it reasonably
        # expects the run to stop, and it does not.
        if self._probe_in_flight:
            self._test_connection_btn.setEnabled(False)
            self._forget_credentials_btn.setEnabled(False)
            self._mpn_status_lbl.setText(status_override or "Checking...")
            return

        if self._enrichment_in_flight():
            self._test_connection_btn.setEnabled(False)
            self._forget_credentials_btn.setEnabled(False)
            self._mpn_status_lbl.setText("Enrichment running.")
            return

        stored = self._mpn_controller.digikey_credentials()

        if not isinstance(stored, Configured):
            self._test_connection_btn.setEnabled(False)
            if isinstance(stored, Partial):
                # A half-entered pair is exactly the state an operator wants
                # to abandon, so Forget stays available here.
                self._forget_credentials_btn.setEnabled(True)
                missing = (
                    "client ID" if stored.missing == MissingCredentialField.CLIENT_ID
                    else "client secret"
                )
                self._mpn_status_lbl.setText(status_override or f"No {missing} stored.")
            else:
                self._forget_credentials_btn.setEnabled(False)
                self._mpn_status_lbl.setText(status_override or "No credentials stored.")
            return

        self._forget_credentials_btn.setEnabled(True)
        api_base = self._mpn_controller.api_base_url()
        if not isinstance(api_base, Accepted):
            self._test_connection_btn.setEnabled(False)
            self._mpn_status_lbl.setText(status_override or BASE_URL_FAULT_TEXT[api_base.fault])
            return

        self._test_connection_btn.setEnabled(True)
        # Naming the host is what turns a mistyped base URL from an invisible
        # disclosure into something an operator reads before clicking.
        self._mpn_status_lbl.setText(
            status_override or f"Credentials will be sent to {api_base.url}."
        )

    def _on_test_connection(self) -> None:
        from cockpit.persistence.clock import utcnow
        from cockpit.settings.mpn_library import Accepted, Configured

        self._commit_digikey_credentials()

        stored = self._mpn_controller.digikey_credentials()
        api_base = self._mpn_controller.api_base_url()
        if not isinstance(stored, Configured) or not isinstance(api_base, Accepted):
            self._refresh_mpn_credential_state()
            return

        credentials = stored.credentials
        host = api_base.url

        def on_result(probe_result) -> None:
            self._probe_in_flight = False
            self._probe_admission = None
            self._refresh_mpn_credential_state(
                status_override=PROBE_RESULT_TEXT[probe_result].format(host=host)
            )

        def on_watchdog() -> None:
            self._probe_in_flight = False
            self._probe_admission = None
            self._refresh_mpn_credential_state(
                status_override=PROBE_RESULT_TEXT[CredentialProbeResult.TIMED_OUT].format(
                    host=host
                )
            )

        def on_error(exc: BaseException) -> None:
            # probe_digikey_credentials returns a result enum for every
            # anticipated failure, so this is a defect path. It reports as
            # unreachable rather than pretending the host answered.
            self._probe_in_flight = False
            self._probe_admission = None
            self._refresh_mpn_credential_state(
                status_override=PROBE_RESULT_TEXT[CredentialProbeResult.UNREACHABLE].format(
                    host=host
                )
            )

        # The credentials are captured as a value before the thread starts, so
        # the dialog can empty its widgets at any moment without disturbing the
        # request in flight.
        self._probe_admission = start_bounded_probe(
            owner=self,
            work=lambda: probe_digikey_credentials(credentials, host, utcnow),
            watchdog_ms=PROBE_WATCHDOG_TIMEOUT_MS,
            on_outcome=on_result,
            on_timeout=on_watchdog,
            on_error=on_error,
        )
        self._probe_in_flight = True
        self._refresh_mpn_credential_state()

    @property
    def _probe_worker(self):
        """The worker behind the current admission, or None.

        Kept as a read path because the dialog's own state checks and Phase 48's
        tests both name it. The thread is retained by the seam's registry, never
        by this.
        """
        admission = getattr(self, "_probe_admission", None)
        return admission.worker if admission is not None else None

    @_probe_worker.setter
    def _probe_worker(self, value):
        # Phase 48's tests simulate a watchdog by clearing this. Honour that by
        # dropping the admission, which is what clearing it meant.
        if value is None:
            self._probe_admission = None

    @property
    def _probe_result_handler(self):
        return self._probe_worker

    @_probe_result_handler.setter
    def _probe_result_handler(self, value):
        if value is None:
            self._probe_admission = None

    def _teardown_credential_probe(self) -> None:
        """Severs a probe from this dialog without waiting on its thread.

        A severed handler has nothing to deliver, which is what makes the
        deleted-widget crash impossible. The thread is asked to exit and ends on
        its own whenever the socket resolves, the worker self-deletes, and the
        result is discarded.
        """
        admission = getattr(self, "_probe_admission", None)
        if admission is not None:
            admission.abandon()
        self._probe_admission = None
        self._probe_in_flight = False

    def _teardown_source_root_probe(self) -> None:
        """Severs the source-root probe on close.

        Without this the probe's handlers survive until the dialog's destroyed
        signal, which is far too late to be doing signal surgery -- and before
        Patch 11 there was no teardown at all, which is how a late result
        reached two deleted widgets.
        """
        admission = getattr(self, "_source_root_admission", None)
        if admission is not None:
            admission.abandon()
        self._source_root_admission = None

    def _update_edit(self) -> None:
        if self._second_ops_controller and self.terms_edit:
            self.terms_edit.setText(", ".join(self._second_ops_controller.terms()))

    def done(self, result_code: int) -> None:
        # Clearing the credential widgets is step one, ahead of the probe
        # teardown and ahead of any queued signal delivery, so nothing that
        # runs later in this method can read a secret out of a live widget.
        # The probe already holds its credentials as a value, so emptying the
        # widgets cannot disturb a request in flight.
        credential_text = self._take_credential_text()
        self._teardown_credential_probe()
        self._teardown_source_root_probe()

        if credential_text is not None and result_code == QDialog.DialogCode.Accepted:
            self._commit_credential_text(*credential_text)

        if self._second_ops_controller and self.terms_edit:
            self._second_ops_controller.set_terms_from_text(self.terms_edit.text())
        super().done(result_code)

    def _take_credential_text(self) -> tuple[str, str] | None:
        """Empties the credential widgets, returning what they held."""
        if self._client_id_edit is None or self._client_secret_edit is None:
            return None
        id_text = self._client_id_edit.text().strip()
        secret_text = self._client_secret_edit.text().strip()
        self._client_id_edit.clear()
        self._client_secret_edit.clear()
        return id_text, secret_text
