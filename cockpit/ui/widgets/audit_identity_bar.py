import html
from typing import Callable, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QPushButton, QWidget

from cockpit.services.requirements import (
    BadgeState,
    EvidenceCache,
    RequirementBadges,
    RequirementEvidence,
    RequirementKind,
    RequirementSummary,
    XrayCellState,
    XrayTick,
    format_requirement_tooltip,
)
from cockpit.services.views import AuditIdentityBanner


def _coloured(text: str, color: str) -> str:
    """One escaped run wrapped in a colour span.

    Every value reaching this function originates in a traveler workbook on a
    share other people write to. Escaping is what keeps a value containing
    markup from being parsed as markup once the label switches to rich text.
    """
    return f'<span style="color:{color};">{html.escape(text)}</span>'


def _class_markup(assembly_class: str, color: str) -> str:
    """Renders "Class 3" with only the digit coloured, any other class plain."""
    escaped = html.escape(assembly_class)
    if not assembly_class.endswith("3"):
        return escaped
    head = html.escape(assembly_class[:-1])
    return f'{head}<span style="color:{color};">3</span>'


class AuditIdentityBar(QWidget):
    back_requested = pyqtSignal()
    xray_toggle_requested = pyqtSignal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._audit_id: int | None = None
        self._evidence_cache = EvidenceCache()
        self._evidence_loader: Callable[[int], list[RequirementEvidence]] = lambda _aid: []
        self._stale_checker: Callable[[int, str], bool] = lambda _aid, _fp: False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(12)

        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self.back_requested.emit)
        layout.addWidget(self.back_btn)

        self.so_lbl = QLabel()
        layout.addWidget(self.so_lbl)
        
        self.pn_lbl = QLabel()
        self.pn_lbl.setProperty("class", "h2 hdr-part-number")
        layout.addWidget(self.pn_lbl)
        
        self.itar_lbl = QLabel("ITAR")
        self.itar_lbl.setProperty("class", "itar-badge")
        layout.addWidget(self.itar_lbl)
        
        self.qty_lbl = QLabel()
        layout.addWidget(self.qty_lbl)

        self.lt_lbl = QLabel()
        layout.addWidget(self.lt_lbl)

        self.class_lbl = QLabel()
        layout.addWidget(self.class_lbl)

        self.process_lbl = QLabel()
        layout.addWidget(self.process_lbl)

        self.aq_lbl = QLabel()
        self.aq_lbl.setProperty("class", "req-chip")
        layout.addWidget(self.aq_lbl)

        self.pf_lbl = QLabel()
        self.pf_lbl.setProperty("class", "req-chip")
        layout.addWidget(self.pf_lbl)

        self.xray_chk = QCheckBox("X-ray & save images")
        self.xray_chk.toggled.connect(self._on_xray_chk_toggled)
        layout.addWidget(self.xray_chk)

        self.xray_lbl = QLabel()
        self.xray_lbl.setProperty("class", "req-chip")
        layout.addWidget(self.xray_lbl)

        self.customer_lbl = QLabel()
        layout.addWidget(self.customer_lbl)

        self.rowc_lbl = QLabel()
        layout.addWidget(self.rowc_lbl)

        self.status_lbl = QLabel()
        layout.addWidget(self.status_lbl)

        layout.addStretch()

        self.set_identity(None)

    def set_evidence_loader(self, loader: Callable[[int], list[RequirementEvidence]]) -> None:
        self._evidence_loader = loader

    def set_stale_checker(self, checker: Callable[[int, str], bool]) -> None:
        self._stale_checker = checker

    def set_stale_scan_checker(self, checker: Callable[[int, str], bool]) -> None:
        self._stale_checker = checker

    def set_audit_id(self, audit_id: int | None) -> None:
        self._audit_id = audit_id

    def _on_xray_chk_toggled(self, checked: bool) -> None:
        if self._audit_id is not None:
            self.xray_toggle_requested.emit(self._audit_id, bool(checked))

    def set_identity(self, data: Optional[AuditIdentityBanner]) -> None:
        if not data:
            self._audit_id = None
            self.so_lbl.setText("")
            self.pn_lbl.setText("")
            self.itar_lbl.hide()
            self.qty_lbl.setText("")
            self.lt_lbl.setText("")
            self.class_lbl.setText("")
            self.process_lbl.setText("")
            self.aq_lbl.setText("")
            self.aq_lbl.hide()
            self.pf_lbl.setText("")
            self.pf_lbl.hide()
            was_blocked = self.xray_chk.blockSignals(True)
            try:
                self.xray_chk.setChecked(False)
            finally:
                self.xray_chk.blockSignals(was_blocked)
            self.xray_chk.setStyleSheet("")
            self.xray_chk.hide()
            self.xray_lbl.setText("")
            self.xray_lbl.hide()
            self.customer_lbl.setText("")
            self.rowc_lbl.setText("")
            self.status_lbl.setText("")
            return

        self.so_lbl.setText(f"{data.sales_order} \xb7" if data.sales_order else "")
        self.pn_lbl.setText(data.part_number)
        
        if data.is_itar:
            self.itar_lbl.show()
        else:
            self.itar_lbl.hide()
            
        self.qty_lbl.setText(f"\xb7 Qty: {data.quantity}" if data.quantity else "")
        self.lt_lbl.setText(f"\xb7 LT: {data.lead_time_days}" if data.lead_time_days else "")
        from cockpit.ui import facelift

        if data.assembly_class:
            self.class_lbl.setText(
                "\xb7 " + _class_markup(data.assembly_class, facelift.palette().overdue)
            )
        else:
            self.class_lbl.setText("")

        self.process_lbl.setText(f"\xb7 {data.process}" if data.process else "")
        self._apply_requirement_badges(data.requirements)
        self.customer_lbl.setText(f"\xb7 {data.customer}" if data.customer else "")

        marker = data.repeat_marker
        if marker.label or marker.reference:
            runs = []
            if marker.label:
                runs.append(html.escape(marker.label))
            if marker.reference:
                runs.append(_coloured(marker.reference, facelift.palette().attention))
            self.rowc_lbl.setText("\xb7 " + " ".join(runs))
        else:
            self.rowc_lbl.setText("")
        self.status_lbl.setText(f"\xb7 Status: {data.status}" if data.status else "")

    def _apply_requirement_badges(self, badges: RequirementBadges) -> None:
        from cockpit.ui import facelift

        if badges.aquanox == BadgeState.DETECTED:
            self.aq_lbl.setText("AQ")
            self.aq_lbl.show()
        elif badges.aquanox in (BadgeState.UNKNOWN, BadgeState.NOT_SCANNED):
            self.aq_lbl.setText("AQ ?")
            self.aq_lbl.show()
        else:
            self.aq_lbl.setText("")
            self.aq_lbl.hide()

        if badges.press_fit == BadgeState.DETECTED:
            self.pf_lbl.setText("PF")
            self.pf_lbl.show()
        elif badges.press_fit in (BadgeState.UNKNOWN, BadgeState.NOT_SCANNED):
            self.pf_lbl.setText("PF ?")
            self.pf_lbl.show()
        else:
            self.pf_lbl.setText("")
            self.pf_lbl.hide()

        x_cell = badges.xray_cell
        if x_cell in (XrayCellState.PENDING, XrayCellState.DONE, XrayCellState.DONE_STALE):
            self.xray_lbl.setText("")
            self.xray_lbl.hide()
            was_blocked = self.xray_chk.blockSignals(True)
            try:
                self.xray_chk.setChecked(x_cell in (XrayCellState.DONE, XrayCellState.DONE_STALE))
            finally:
                self.xray_chk.blockSignals(was_blocked)
            if x_cell == XrayCellState.DONE_STALE:
                amber = facelift.palette().due_soon
                self.xray_chk.setStyleSheet(
                    f"QCheckBox {{ color: {amber}; }} "
                    f"QCheckBox::indicator:checked {{ border: 2px solid {amber}; background-color: {amber}; }}"
                )
                self.xray_chk.setProperty("xray_stale", True)
            else:
                self.xray_chk.setStyleSheet("")
                self.xray_chk.setProperty("xray_stale", False)
            self.xray_chk.show()
        elif x_cell == XrayCellState.XR_ONLY:
            self.xray_chk.hide()
            self.xray_lbl.setText("XR")
            self.xray_lbl.show()
        elif x_cell in (XrayCellState.UNKNOWN, XrayCellState.NOT_SCANNED):
            self.xray_chk.hide()
            self.xray_lbl.setText("X-ray ?")
            self.xray_lbl.show()
        else:
            self.xray_chk.hide()
            self.xray_lbl.setText("")
            self.xray_lbl.hide()

    def set_requirements_context(
        self,
        audit_id: int | None,
        summary: RequirementSummary | None,
        tick: XrayTick = XrayTick(),
    ) -> None:
        self._audit_id = audit_id
        if audit_id is None:
            self.aq_lbl.setToolTip("")
            self.pf_lbl.setToolTip("")
            self.xray_chk.setToolTip("")
            self.xray_lbl.setToolTip("")
            return

        if summary is None:
            scanning_tip = format_requirement_tooltip(kinds=(), summary=None) or ""
            self.aq_lbl.setToolTip(scanning_tip)
            self.pf_lbl.setToolTip(scanning_tip)
            self.xray_chk.setToolTip(scanning_tip)
            self.xray_lbl.setToolTip(scanning_tip)
            return

        ev_list = self._evidence_cache.get(
            audit_id, summary.fingerprint, self._evidence_loader
        )
        is_stale = self._stale_checker(audit_id, summary.fingerprint)

        aq_tip = format_requirement_tooltip(
            kinds=(RequirementKind.AQUANOX,),
            summary=summary,
            evidence=ev_list,
            is_stale_scan=is_stale,
        ) or ""
        pf_tip = format_requirement_tooltip(
            kinds=(RequirementKind.PRESS_FIT,),
            summary=summary,
            evidence=ev_list,
            is_stale_scan=is_stale,
        ) or ""
        xray_tip = format_requirement_tooltip(
            kinds=(RequirementKind.XRAY_SAVE_IMAGES, RequirementKind.XRAY),
            summary=summary,
            tick=tick,
            evidence=ev_list,
            is_xray_column=True,
            is_stale_scan=is_stale,
        ) or ""

        self.aq_lbl.setToolTip(aq_tip)
        self.pf_lbl.setToolTip(pf_tip)
        self.xray_chk.setToolTip(xray_tip)
        self.xray_lbl.setToolTip(xray_tip)
