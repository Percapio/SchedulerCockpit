"""Settings dialog (Phase 32, 2.4 + 3.2)."""

import datetime
import logging
import pathlib
from dataclasses import dataclass
from functools import partial

from PyQt6.QtCore import pyqtSignal, Qt, QObject
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QComboBox, QFontComboBox,
    QSpinBox, QDoubleSpinBox, QGroupBox, QPushButton, QLabel, QFileDialog, QMessageBox, QWidget,
    QAbstractSpinBox, QLineEdit
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

logger = logging.getLogger(__name__)

_PRESET_LABELS = {facelift.DARK: "Dark", facelift.LIGHT: "Light"}

# Longer than the gateway's CONNECT_TIMEOUT_S so an ordinary connect timeout
# reports itself rather than being pre-empted by the watchdog. The watchdog is
# there for the proxy that accepts the connection and never answers.
PROBE_WATCHDOG_TIMEOUT_MS = 25_000

# Credential probes outlive the dialog that started them. A dialog closed
# mid-probe disconnects its handler and returns without waiting, so the thread
# reference has to live somewhere that is not a widget: Python collecting a
# running QThread is a crash, not a leak. Entries are discarded on finished.
_IN_FLIGHT_PROBES: set = set()


class _CredentialProbeWorker(QObject):
    """Runs one DigiKey token request off the UI thread.

    Holds its credentials as a value handed over before the thread started, so
    the dialog can empty its widgets at any moment without touching the
    request in flight.
    """

    finished = pyqtSignal(object)  # CredentialProbeResult

    def __init__(self, credentials, api_base: str):
        super().__init__()
        self._credentials = credentials
        self._api_base = api_base

    def run(self) -> None:
        from cockpit.persistence.clock import utcnow
        self.finished.emit(
            probe_digikey_credentials(self._credentials, self._api_base, utcnow)
        )

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




class SettingsDialog(QDialog):
    """Display preferences (theme preset, font family, font size) + diagnostics export.

    Changes apply live and persist via the controllers' QSettings backing.
    """

    reset_requested = pyqtSignal()

    def __init__(
        self,
        style_controller: StyleController,
        font_scale_controller: FontScaleController,
        runtime_settings_controller: RuntimeCalcSettingsController | None,
        second_ops_settings_controller, # from cockpit.services.second_ops import SecondOpsSettingsController
        config,
        parent: QWidget | None = None,
        source_root_controller=None,
        mpn_library_controller=None,
        enrichment_in_flight=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._style = style_controller
        self._font_scale = font_scale_controller
        self._config = config
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
        self._probe_worker = None
        self._probe_result_handler = None
        # Supplied by MainWindow, which is the only object that can see a
        # running enrichment. Absent in tests that build the dialog alone.
        self._enrichment_in_flight = enrichment_in_flight or (lambda: False)

        layout = QVBoxLayout(self)
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
        layout.addLayout(buttons)

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
        # Maybe use small font?
        layout.addWidget(info)
        
        return group

    def _build_source_root_settings(self, controller) -> QGroupBox:
        from cockpit.settings.source_root import SourceRootState, probe_source_root, RootProbeResult
        from PyQt6.QtCore import QThread, pyqtSignal, QObject
        
        class ProbeWorker(QObject):
            finished = pyqtSignal(object)
            def __init__(self, path: pathlib.Path):
                super().__init__()
                self.path = path
            def run(self):
                self.finished.emit(probe_source_root(self.path))
                
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
        
        self._probe_thread = None
        self._probe_worker = None
        
        def on_validate():
            current_state, current_path = controller.source_root()
            if current_state != SourceRootState.CONFIGURED:
                status_lbl.setText("Error: Path must be absolute.")
                return
                
            validate_btn.setEnabled(False)
            status_lbl.setText("Checking...")
            
            self._probe_thread = QThread()
            self._probe_worker = ProbeWorker(current_path)
            self._probe_worker.moveToThread(self._probe_thread)
            self._probe_thread.started.connect(self._probe_worker.run)
            
            def on_finished(result: RootProbeResult):
                validate_btn.setEnabled(True)
                if result == RootProbeResult.REACHABLE:
                    status_lbl.setText("Reachable.")
                elif result == RootProbeResult.REACHABLE_BUT_NO_JOB_TREE:
                    status_lbl.setText("Reachable, but no job tree found.")
                else:
                    status_lbl.setText("Unreachable.")
                self._probe_thread.quit()
                self._probe_thread.wait()
                
            self._probe_worker.finished.connect(on_finished)
            self._probe_thread.start()
            
        validate_btn.clicked.connect(on_validate)
        return group

    def _build_mpn_library_settings(self, controller) -> QGroupBox:
        from PyQt6.QtWidgets import QCheckBox, QSpinBox, QFrame
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
        from PyQt6.QtCore import QThread, QTimer
        from cockpit.settings.mpn_library import Accepted, Configured

        self._commit_digikey_credentials()

        stored = self._mpn_controller.digikey_credentials()
        api_base = self._mpn_controller.api_base_url()
        if not isinstance(stored, Configured) or not isinstance(api_base, Accepted):
            self._refresh_mpn_credential_state()
            return

        thread = QThread()
        worker = _CredentialProbeWorker(stored.credentials, api_base.url)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        entry = (thread, worker)
        # A probe outlives the dialog that started it, and two can be in
        # flight once the watchdog fires and the operator clicks again, so
        # the references live here rather than in a single slot on the dialog.
        _IN_FLIGHT_PROBES.add(entry)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: _IN_FLIGHT_PROBES.discard(entry))
        thread.finished.connect(thread.deleteLater)

        watchdog = QTimer(self)
        watchdog.setSingleShot(True)

        def on_result(probe_result) -> None:
            watchdog.stop()
            self._probe_in_flight = False
            self._probe_worker = None
            self._probe_result_handler = None
            self._refresh_mpn_credential_state(
                status_override=PROBE_RESULT_TEXT[probe_result].format(host=api_base.url)
            )

        def on_watchdog() -> None:
            # Abandonment, not cancellation: a blocking socket read cannot be
            # interrupted from the UI thread, and wait() here would reproduce
            # the freeze this timer exists to escape.
            try:
                worker.finished.disconnect(on_result)
            except TypeError:
                return
            self._probe_in_flight = False
            self._probe_worker = None
            self._probe_result_handler = None
            self._refresh_mpn_credential_state(
                status_override=PROBE_RESULT_TEXT[CredentialProbeResult.TIMED_OUT].format(
                    host=api_base.url
                )
            )

        worker.finished.connect(on_result)
        watchdog.timeout.connect(on_watchdog)

        self._probe_in_flight = True
        self._probe_worker = worker
        self._probe_result_handler = on_result
        self._refresh_mpn_credential_state()
        watchdog.start(PROBE_WATCHDOG_TIMEOUT_MS)
        thread.start()

    def _teardown_credential_probe(self) -> None:
        """Disconnects a probe from this dialog without waiting on its thread.

        A disconnected signal has nothing to deliver, which is what makes the
        deleted-widget crash impossible. The thread ends on its own whenever
        the socket resolves, the worker self-deletes, and the result is
        discarded.
        """
        if self._probe_worker is not None and self._probe_result_handler is not None:
            try:
                self._probe_worker.finished.disconnect(self._probe_result_handler)
            except TypeError:
                pass
        self._probe_worker = None
        self._probe_result_handler = None
        self._probe_in_flight = False

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
