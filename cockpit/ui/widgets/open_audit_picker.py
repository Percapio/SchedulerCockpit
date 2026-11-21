"""Open audit picker."""

import html
import logging
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta, date
from typing import Sequence, Any, List, Callable, Mapping
from enum import IntEnum
from PyQt6.QtCore import Qt, pyqtSignal, QAbstractTableModel, QModelIndex, QSortFilterProxyModel, QSettings, QByteArray, QRect, QEvent, QTimer, QPointF
from PyQt6.QtGui import QFont, QColor, QPen, QPainter, QPalette
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTableView, QHeaderView, QStyledItemDelegate, QMenu, QLineEdit,
    QStyle, QStyleOptionViewItem, QStyleOptionButton, QApplication, QComboBox, QDialog, QDialogButtonBox, QMessageBox
)

from cockpit.persistence.types import AuditStatus
from cockpit.persistence.traveler_flags import WashState, is_class_3, wash_state
from cockpit.services.company_names import company_display_name
from cockpit.services.requirements import (
    BadgeState,
    EvidenceCache,
    RequirementEvidence,
    RequirementKind,
    RequirementState,
    Result,
    XrayCellState,
    format_requirement_tooltip,
    requirement_badge,
    xray_cell_state,
)
from cockpit.services.views import OpenAuditDigest
from cockpit.ui.audit_list_views import (
    MAX_SAVED_VIEWS,
    ColumnLayout,
    ColumnPlacement,
    ColumnSpec,
    SavedView,
    default_layout,
    load_views,
    reconcile_layout,
    store_views,
    validate_view_name,
    view_name_refusal_text,
)
from cockpit.ui.widgets.hand_flag_menu import flag_menu_title, flag_request_of, populate_hand_flag_menu
from cockpit.ui.widgets.toast import Toast  # Phase 3

logger = logging.getLogger(__name__)

# Phase 59 §6: the column layout lives in named views (cockpit/ui/audit_list_views.py), keyed by
# Column member name. The pre-Phase 59 header state is carried over once and its keys removed.
XRAY_STALE_ROLE = int(Qt.ItemDataRole.UserRole) + 1


def _format_list_date(value: date | None) -> str:
    return "" if value is None else f"{value:%b} {value.day}"


def _article_text(digest) -> str:
    """NULL and "FA" both mean first article; only NULL reaches an old row."""
    return digest.article_revision or FIRST_ARTICLE_TEXT


def _centered_square(cell: QRect, extent: int) -> QRect:
    return QRect(
        cell.x() + (cell.width() - extent) // 2,
        cell.y() + (cell.height() - extent) // 2,
        extent,
        extent
    )


PST = timezone(timedelta(hours=-8))


class Column(IntEnum):
    """Logical indices. INV-C1 (Phase 59 §6.1): an integer never changes; a new column takes the next
    one, and its default position comes from COLUMN_SPECS' order. The member names are stored in
    views, so renaming one is a format change."""
    START_BY = 0
    SHIP_DATE = 1
    PART_NUMBER = 2
    WORK_ORDER_REF = 3
    LEAD_TIME = 4
    QUANTITY = 5
    REPEAT = 6
    CLASSIFICATION = 7
    ASSEMBLY_CLASS = 8
    PROCESS = 9
    WASH = 10
    AQUANOX = 11
    PRESS_FIT = 12
    FSU = 13
    SMT = 14
    THT = 15
    AOI = 16
    OPS = 17
    SHIP = 18
    DATE_INGESTED = 19
    LABEL = 20
    PHOTOS = 21
    XRAY = 22
    ARTICLE_REVISION = 23
    COMPANY = 24


# Phase 49 (2.2): no Audit List code compares a column against an integer
# literal. Column order is a property of the enum alone.
_SEMANTIC_ROLE_BY_COLUMN = {
    Column.PART_NUMBER: "part_number",
    Column.QUANTITY: "quantity",
    Column.PROCESS: "process",
}

# Blank is a real answer: a traveler ingested before process_clean was mapped
# said nothing about washing, and "NC" would put words in its mouth.
FIRST_ARTICLE_TEXT = "FA"

WASH_COLUMN_TEXT = {
    WashState.CLEAN: "C",
    WashState.NO_CLEAN: "NC",
    WashState.UNKNOWN: "",
    # An off-template traveler reads as blank, same as one that said nothing.
    # The distinction lives in the log written at ingest, not in the column:
    # surfacing it here adds UI for a case the dropdown should make impossible.
    WashState.UNRECOGNISED: "",
}
WASH_SEARCH_TOKEN = {
    WashState.CLEAN: "wash",
    WashState.NO_CLEAN: "noclean",
    WashState.UNKNOWN: "",
    WashState.UNRECOGNISED: "",
}

_HEADER_TOOLTIPS: dict[Column, str] = {
    Column.WASH: "The traveler's flux designation",
    Column.AQUANOX: "Chemical or solvent wash required by the ECO, BOM or traveler; AQ* is your hand flag",
    Column.PRESS_FIT: "Press-fit parts or instructions; PF* is your hand flag",
    Column.XRAY: "The X-ray & save-images task; XR means X-ray without saving images",
    Column.COMPANY: "The traveler's customer; Settings › Company Names gives short names",
}

_CHECKABLE_XRAY_STATES = frozenset({
    XrayCellState.PENDING,
    XrayCellState.DONE,
    XrayCellState.DONE_STALE,
})

_XRAY_SORT_RANK: dict[XrayCellState, int] = {
    XrayCellState.PENDING: 0,
    XrayCellState.DONE_STALE: 1,
    XrayCellState.DONE: 2,
    XrayCellState.XR_ONLY: 3,
    XrayCellState.UNKNOWN: 4,
    XrayCellState.NOT_SCANNED: 4,
    XrayCellState.BLANK: 5,
}


def wash_state_of(digest) -> WashState:
    return wash_state({"process_clean": digest.process_clean})


def _req_badge(digest: OpenAuditDigest, kind: RequirementKind) -> BadgeState:
    """Phase 59 §4.4: the documents' state, or FLAGGED when your hand flag alone carries it."""
    return requirement_badge(digest.requirements, kind, getattr(digest, "hand_flags", None))


def _req_badge_text(digest: OpenAuditDigest, kind: RequirementKind, code: str) -> str:
    state = _req_badge(digest, kind)
    if state == BadgeState.DETECTED:
        return code
    if state == BadgeState.FLAGGED:
        return f"{code}*"
    if state in (BadgeState.UNKNOWN, BadgeState.NOT_SCANNED):
        return "?"
    return ""


def _req_sort_rank(digest: OpenAuditDigest, kind: RequirementKind) -> int:
    state = _req_badge(digest, kind)
    if state in (BadgeState.DETECTED, BadgeState.FLAGGED):
        return 0
    if state in (BadgeState.UNKNOWN, BadgeState.NOT_SCANNED):
        return 1
    return 2


_CHECKBOX_COLUMNS = frozenset({Column.LABEL, Column.PHOTOS, Column.XRAY})
_STATUS_STAGE_COLUMN = {
    AuditStatus.FSU: Column.FSU,
    AuditStatus.SMT: Column.SMT,
    AuditStatus.THT: Column.THT,
    AuditStatus.AOI: Column.AOI,
    AuditStatus.OPS: Column.OPS,
    AuditStatus.SHIPPING: Column.SHIP,
}
_DEFAULT_COLUMN_WIDTHS: dict[Column, int] = {
    Column.START_BY: 90, Column.SHIP_DATE: 90, Column.PART_NUMBER: 120, Column.WORK_ORDER_REF: 80,
    Column.LEAD_TIME: 50, Column.QUANTITY: 55, Column.REPEAT: 70, Column.CLASSIFICATION: 110,
    Column.ASSEMBLY_CLASS: 55, Column.PROCESS: 90, Column.WASH: 55,
    Column.AQUANOX: 40, Column.PRESS_FIT: 40,
    Column.FSU: 70, Column.SMT: 70, Column.THT: 70,
    Column.AOI: 70, Column.OPS: 70, Column.SHIP: 70, Column.DATE_INGESTED: 100,
    Column.LABEL: 55, Column.PHOTOS: 60, Column.XRAY: 55, Column.ARTICLE_REVISION: 70,
    Column.COMPANY: 140,
}
_DATE_COLUMNS = frozenset({Column.START_BY, Column.SHIP_DATE})

# Indexed by Column (INV-C1), so a new title goes last.
COLUMN_TITLES: tuple[str, ...] = (
    "Start-By Date", "Ship Date", "B#", "S/O", "LT",
    "QTY", "Type", "Classification", "Class", "Process", "Wash",
    "AQ", "PF",
    "FSU (hrs)", "SMT (hrs)", "THT (hrs)",
    "AOI (hrs)", "OPS (hrs)", "SHIP (hrs)", "Date Ingested",
    "Label", "Photos", "X-ray", "Article",
    "Company",
)

# Phase 59 §6.1: the default visual order. INV-C2: Start-By is first, always shown, and not in the
# column menu, because group-header rows span from it and draw their label in it.
_DEFAULT_ORDER: tuple[Column, ...] = (
    Column.START_BY, Column.SHIP_DATE, Column.PART_NUMBER, Column.WORK_ORDER_REF, Column.COMPANY,
    Column.LEAD_TIME, Column.QUANTITY, Column.REPEAT, Column.CLASSIFICATION, Column.ASSEMBLY_CLASS,
    Column.PROCESS, Column.WASH, Column.AQUANOX, Column.PRESS_FIT,
    Column.FSU, Column.SMT, Column.THT, Column.AOI, Column.OPS, Column.SHIP, Column.DATE_INGESTED,
    Column.LABEL, Column.PHOTOS, Column.XRAY, Column.ARTICLE_REVISION,
)
assert sorted(_DEFAULT_ORDER) == sorted(Column) and len(COLUMN_TITLES) == len(Column)

COLUMN_SPECS: tuple[ColumnSpec, ...] = tuple(
    ColumnSpec(
        key=column.name,
        title=COLUMN_TITLES[column],
        default_width_px=_DEFAULT_COLUMN_WIDTHS[column],
        default_visible=True,
        hideable=column != Column.START_BY,
    )
    for column in _DEFAULT_ORDER
)


class RowKind:
    DATA = "data"
    GROUP_HEADER = "group_header"


class AuditListModel(QAbstractTableModel):
    label_toggle_requested = pyqtSignal(int, bool)
    photos_toggle_requested = pyqtSignal(int, bool)
    xray_toggle_requested = pyqtSignal(int, bool)

    COLUMNS = list(COLUMN_TITLES)

    GROUP_ORDER = AuditStatus.ordered()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._digests: List[OpenAuditDigest] = []
        self._rows: List[dict] = []
        self._sort_col = 0  # Start-By Date default
        self._sort_asc = True
        self._evidence_cache = EvidenceCache()
        self._evidence_loader: Callable[[int], list[RequirementEvidence]] = lambda _aid: []
        self._stale_fingerprint_checker: Callable[[OpenAuditDigest], bool] = lambda _d: False
        self._stale_scan_checker: Callable[[int, str], bool] | None = None
        # Phase 59 §5.2: the short-name map the Company cell, its sort and the search read.
        self._company_names: dict = {}

    def set_company_names(self, names: Mapping) -> None:
        """Takes the current short-name map; the caller repopulates to show it."""
        self._company_names = dict(names or {})

    def company_names(self) -> dict:
        return self._company_names

    def company_text(self, digest: OpenAuditDigest) -> str:
        return company_display_name(getattr(digest, "customer", ""), self._company_names)

    def set_evidence_loader(self, loader: Callable[[int], list[RequirementEvidence]]) -> None:
        self._evidence_loader = loader

    def set_stale_fingerprint_checker(self, checker: Callable[[OpenAuditDigest], bool]) -> None:
        self._stale_fingerprint_checker = checker

    def set_stale_scan_checker(self, checker: Callable[[int, str], bool]) -> None:
        self._stale_scan_checker = checker

    def set_sort(self, column: int, ascending: bool):
        self._sort_col = column
        self._sort_asc = ascending
        self.rebuild(self._digests)

    def rebuild(self, digests: Sequence[OpenAuditDigest]) -> None:
        self.beginResetModel()
        self._digests = list(digests)
        self._rows = []
        
        buckets = {st: [] for st in self.GROUP_ORDER}
        for d in self._digests:
            # any status not in groups gets put into NOT_CLEAR fallback
            bucket_key = d.status if d.status in buckets else AuditStatus.NOT_CLEAR
            buckets[bucket_key].append(d)
            
        for st in self.GROUP_ORDER:
            bucket = buckets[st]
            if not bucket:
                continue
                
            bucket.sort(key=self._sort_key, reverse=not self._sort_asc)
            
            self._rows.append({
                "kind": RowKind.GROUP_HEADER,
                "label": st.value
            })
            for d in bucket:
                self._rows.append({
                    "kind": RowKind.DATA,
                    "digest": d
                })
                
        self.endResetModel()

    def _sort_key(self, d: OpenAuditDigest) -> Any:
        col = self._sort_col
        if col == Column.START_BY: return (d.start_by is not None, d.start_by)
        if col == Column.SHIP_DATE: return (d.ship_date is not None, d.ship_date)
        if col == Column.PART_NUMBER: return str(d.part_number)
        if col == Column.WORK_ORDER_REF: return f"{d.work_order_ref}{d.split_suffix}"
        if col == Column.LEAD_TIME: return (d.lead_time_days is not None, d.lead_time_days)
        if col == Column.QUANTITY: return d.quantity
        if col == Column.REPEAT: return d.repeat.display
        if col == Column.CLASSIFICATION: return str(d.classification)
        if col == Column.ASSEMBLY_CLASS: return (d.assembly_class is not None, d.assembly_class)
        if col == Column.PROCESS: return str(d.process)
        if col == Column.WASH: return WASH_COLUMN_TEXT[wash_state_of(d)]
        if col == Column.AQUANOX: return _req_sort_rank(d, RequirementKind.AQUANOX)
        if col == Column.PRESS_FIT: return _req_sort_rank(d, RequirementKind.PRESS_FIT)
        if col == Column.FSU: return (d.feeder_setuptime is not None, d.feeder_setuptime)
        if col == Column.SMT: return (d.smt_runtime is not None, d.smt_runtime)
        if col == Column.THT: return (d.tht_runtime is not None, d.tht_runtime)
        if col == Column.AOI: return (d.aoi_runtime is not None, d.aoi_runtime)
        if col == Column.OPS: return (d.ops_runtime is not None, d.ops_runtime)
        if col == Column.SHIP: return (d.shipping_runtime is not None, d.shipping_runtime)
        if col == Column.DATE_INGESTED: return (d.date_ingested is not None, d.date_ingested)
        if col == Column.LABEL: return d.is_labeled
        if col == Column.PHOTOS: return d.are_photos_uploaded
        if col == Column.XRAY: return _XRAY_SORT_RANK[xray_cell_state(d.requirements, d.xray_tick)]
        if col == Column.ARTICLE_REVISION: return _article_text(d)
        if col == Column.COMPANY:
            shown = self.company_text(d)
            return (shown != "", shown.casefold())
        return 0

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        row_data = self._rows[index.row()]
        if row_data["kind"] == RowKind.GROUP_HEADER:
            return Qt.ItemFlag.ItemIsEnabled
        col = index.column()
        if col in (Column.LABEL, Column.PHOTOS):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        if col == Column.XRAY:
            d: OpenAuditDigest = row_data["digest"]
            if xray_cell_state(d.requirements, d.xray_tick) in _CHECKABLE_XRAY_STATES:
                return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            return super().flags(index)
        return super().flags(index)

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
            
        row_data = self._rows[index.row()]
        
        if role == Qt.ItemDataRole.UserRole:
            return row_data
            
        if row_data["kind"] == RowKind.GROUP_HEADER:
            if role == Qt.ItemDataRole.DisplayRole and index.column() == 0:
                return row_data["label"]
            return None
            
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if row_data["kind"] == RowKind.DATA:
                return Qt.AlignmentFlag.AlignCenter

        if role == XRAY_STALE_ROLE:
            if row_data["kind"] == RowKind.DATA and index.column() == Column.XRAY:
                d: OpenAuditDigest = row_data["digest"]
                return xray_cell_state(d.requirements, d.xray_tick) == XrayCellState.DONE_STALE
            return False

        if role == Qt.ItemDataRole.CheckStateRole:
            if row_data["kind"] == RowKind.DATA:
                col = index.column()
                d: OpenAuditDigest = row_data["digest"]
                if col == Column.LABEL:
                    return Qt.CheckState.Checked if d.is_labeled else Qt.CheckState.Unchecked
                if col == Column.PHOTOS:
                    return Qt.CheckState.Checked if d.are_photos_uploaded else Qt.CheckState.Unchecked
                if col == Column.XRAY:
                    x_st = xray_cell_state(d.requirements, d.xray_tick)
                    if x_st == XrayCellState.PENDING:
                        return Qt.CheckState.Unchecked
                    if x_st in (XrayCellState.DONE, XrayCellState.DONE_STALE):
                        return Qt.CheckState.Checked
                    return None
            return None

        if role == Qt.ItemDataRole.ToolTipRole and row_data["kind"] == RowKind.DATA:
            col = index.column()
            if col == Column.COMPANY:
                # Phase 59 §5.1: the full name, escaped, since Qt renders tooltip markup as rich text.
                customer = getattr(row_data["digest"], "customer", "")
                return f"<span>{html.escape(customer)}</span>" if customer else None
            if col in (Column.AQUANOX, Column.PRESS_FIT, Column.XRAY):
                d: OpenAuditDigest = row_data["digest"]
                if d.requirements is None:
                    flag_kind = {Column.AQUANOX: RequirementKind.AQUANOX,
                                 Column.PRESS_FIT: RequirementKind.PRESS_FIT}.get(Column(col))
                    return format_requirement_tooltip(
                        kinds=(),
                        summary=None,
                        hand_flag=(getattr(d, "hand_flags", None) or {}).get(flag_kind) if flag_kind else None,
                    )
                ev_list = self._evidence_cache.get(
                    d.audit_id,
                    d.requirements.fingerprint,
                    self._evidence_loader,
                )
                if self._stale_scan_checker is not None:
                    is_stale = self._stale_scan_checker(d.audit_id, d.requirements.fingerprint)
                else:
                    is_stale = self._stale_fingerprint_checker(d)
                flags = getattr(d, "hand_flags", None) or {}
                if col == Column.AQUANOX:
                    return format_requirement_tooltip(
                        kinds=(RequirementKind.AQUANOX,),
                        summary=d.requirements,
                        evidence=ev_list,
                        is_stale_scan=is_stale,
                        hand_flag=flags.get(RequirementKind.AQUANOX),
                    )
                if col == Column.PRESS_FIT:
                    return format_requirement_tooltip(
                        kinds=(RequirementKind.PRESS_FIT,),
                        summary=d.requirements,
                        evidence=ev_list,
                        is_stale_scan=is_stale,
                        hand_flag=flags.get(RequirementKind.PRESS_FIT),
                    )
                if col == Column.XRAY:
                    return format_requirement_tooltip(
                        kinds=(RequirementKind.XRAY_SAVE_IMAGES, RequirementKind.XRAY),
                        summary=d.requirements,
                        tick=d.xray_tick,
                        evidence=ev_list,
                        is_xray_column=True,
                        is_stale_scan=is_stale,
                    )
            return None

        if role == Qt.ItemDataRole.FontRole and row_data["kind"] == RowKind.DATA:
            if index.column() in _DATE_COLUMNS:
                d: OpenAuditDigest = row_data["digest"]
                val = d.start_by if index.column() == Column.START_BY else d.ship_date
                if val is not None:
                    font = QFont()
                    font.setBold(True)
                    return font
            return None

        # Phase 32 (2.5): semantic highlighting for B#, QTY, Process columns.
        if role == Qt.ItemDataRole.ForegroundRole and row_data["kind"] == RowKind.DATA:
            from cockpit.ui import facelift
            from cockpit.services.date_urgency import DateUrgency
            d: OpenAuditDigest = row_data["digest"]
            if index.column() in _DATE_COLUMNS:
                urgency = d.start_by_urgency if index.column() == Column.START_BY else d.ship_urgency
                if urgency == DateUrgency.OVERDUE:
                    return facelift.overdue_color()
                elif urgency == DateUrgency.DUE_SOON:
                    return facelift.due_soon_color()
                return None

            if index.column() == _STATUS_STAGE_COLUMN.get(d.status):
                return facelift.attention_color()
            if index.column() == Column.CLASSIFICATION and d.is_itar:
                return facelift.attention_color()
            # Phase 49 (4.5): the cell holds nothing but the digit, so colouring
            # the whole cell colours exactly the intended glyph.
            if index.column() == Column.ASSEMBLY_CLASS and is_class_3(
                {"assembly_class": d.assembly_class}
            ):
                return facelift.overdue_color()
            
            color = facelift.semantic_color(_SEMANTIC_ROLE_BY_COLUMN.get(index.column()))
            if color is not None:
                return color

        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col in (Column.LABEL, Column.PHOTOS):
                return None
            d: OpenAuditDigest = row_data["digest"]
            if col == Column.XRAY:
                x_st = xray_cell_state(d.requirements, d.xray_tick)
                if x_st in _CHECKABLE_XRAY_STATES:
                    return None
                if x_st == XrayCellState.XR_ONLY:
                    return "XR"
                if x_st in (XrayCellState.UNKNOWN, XrayCellState.NOT_SCANNED):
                    return "?"
                return ""
            
            if col == Column.START_BY: return _format_list_date(d.start_by)
            if col == Column.SHIP_DATE: return _format_list_date(d.ship_date)
            if col == Column.PART_NUMBER: return d.part_number
            if col == Column.WORK_ORDER_REF: return f"{d.work_order_ref}{d.split_suffix}"
            if col == Column.LEAD_TIME: return str(d.lead_time_days) if d.lead_time_days is not None else ""
            if col == Column.QUANTITY: return str(d.quantity)
            if col == Column.REPEAT: return d.repeat.display
            if col == Column.CLASSIFICATION: return d.classification
            if col == Column.ASSEMBLY_CLASS: return str(d.assembly_class) if d.assembly_class is not None else ""
            if col == Column.PROCESS: return d.process or ""
            if col == Column.WASH: return WASH_COLUMN_TEXT[wash_state_of(d)]
            if col == Column.AQUANOX: return _req_badge_text(d, RequirementKind.AQUANOX, "AQ")
            if col == Column.PRESS_FIT: return _req_badge_text(d, RequirementKind.PRESS_FIT, "PF")
            if col == Column.FSU: return f"{d.feeder_setuptime:.1f}" if d.feeder_setuptime is not None else ""
            if col == Column.SMT: return f"{d.smt_runtime:.1f}" if d.smt_runtime is not None else ""
            if col == Column.THT: return f"{d.tht_runtime:.1f}" if d.tht_runtime is not None else ""
            if col == Column.AOI: return f"{d.aoi_runtime:.1f}" if d.aoi_runtime is not None else ""
            if col == Column.OPS: return f"{d.ops_runtime:.1f}" if d.ops_runtime is not None else ""
            if col == Column.SHIP: return f"{d.shipping_runtime:.1f}" if d.shipping_runtime is not None else ""
            if col == Column.DATE_INGESTED:
                if not d.date_ingested: return ""
                local = d.date_ingested.astimezone(PST)
                return f"{local:%Y-%m-%d}"
            if col == Column.ARTICLE_REVISION: return _article_text(d)
            if col == Column.COMPANY: return self.company_text(d)

        return None

    def setData(self, index: QModelIndex, value: Any, role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid():
            return False
        row_data = self._rows[index.row()]
        if row_data["kind"] != RowKind.DATA:
            return False
        if role == Qt.ItemDataRole.CheckStateRole and index.column() in _CHECKBOX_COLUMNS:
            d: OpenAuditDigest = row_data["digest"]
            checked = (value == Qt.CheckState.Checked.value or value == Qt.CheckState.Checked or value == 2 or value is True)
            if index.column() == Column.LABEL:
                self.label_toggle_requested.emit(d.audit_id, checked)
                return True
            if index.column() == Column.PHOTOS:
                self.photos_toggle_requested.emit(d.audit_id, checked)
                return True
            if index.column() == Column.XRAY:
                if xray_cell_state(d.requirements, d.xray_tick) not in _CHECKABLE_XRAY_STATES:
                    return False
                self.xray_toggle_requested.emit(d.audit_id, checked)
                return True
        return super().setData(index, value, role)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.COLUMNS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
            if role == Qt.ItemDataRole.ToolTipRole:
                try:
                    return _HEADER_TOOLTIPS.get(Column(section))
                except ValueError:
                    return None
        return None


class CenteredCheckDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if not row_data or row_data.get("kind") != RowKind.DATA:
            super().paint(painter, option, index)
            return
        state = index.data(Qt.ItemDataRole.CheckStateRole)
        if state is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        opt.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, option.widget)
        
        indicator = QStyleOptionButton()
        extent = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, option, option.widget)
        indicator.rect = _centered_square(option.rect, extent)
        indicator_state = QStyle.StateFlag.State_Enabled
        is_checked = (state == Qt.CheckState.Checked or state == Qt.CheckState.Checked.value or state == 2 or state is True)
        is_stale = bool(index.data(XRAY_STALE_ROLE))
        if is_checked and not is_stale:
            indicator_state |= QStyle.StateFlag.State_On
        else:
            indicator_state |= QStyle.StateFlag.State_Off
        indicator.state = indicator_state
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, indicator, painter, option.widget)

        if is_checked and is_stale:
            from cockpit.ui import facelift
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            pen = QPen(facelift.due_soon_color(), 2.0)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            r = indicator.rect
            p1 = QPointF(r.left() + r.width() * 0.22, r.top() + r.height() * 0.52)
            p2 = QPointF(r.left() + r.width() * 0.44, r.top() + r.height() * 0.74)
            p3 = QPointF(r.left() + r.width() * 0.78, r.top() + r.height() * 0.28)
            painter.drawLine(p1, p2)
            painter.drawLine(p2, p3)
            painter.restore()

    def editorEvent(self, event, model, option, index):
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if not row_data or row_data.get("kind") != RowKind.DATA:
            return False
        current = index.data(Qt.ItemDataRole.CheckStateRole)
        if current is None:
            return False
        toggled = False
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton and option.rect.contains(event.pos()):
            toggled = True
        elif event.type() == QEvent.Type.KeyPress and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            toggled = True
        if not toggled:
            return False
        is_checked = (current == Qt.CheckState.Checked or current == Qt.CheckState.Checked.value or current == 2 or current is True)
        next_state = Qt.CheckState.Unchecked if is_checked else Qt.CheckState.Checked
        return model.setData(index, next_state, Qt.ItemDataRole.CheckStateRole)


class RepeatReferenceDelegate(QStyledItemDelegate):
    """Paints the repeat cell as two runs so the reference alone carries the cue.

    The split is taken from the RepeatMarker, never by searching the displayed
    string for a separator: nothing constrains rowc_label to a single word.
    A row whose display text does not end in its reference — a NEW assembly,
    or a synthesised "REPEAT" — is drawn exactly as the default delegate would.
    """

    def paint(self, painter, option, index):
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if not row_data or row_data.get("kind") != RowKind.DATA:
            super().paint(painter, option, index)
            return

        digest: OpenAuditDigest = row_data["digest"]
        marker = digest.repeat
        reference = marker.reference
        display = marker.display

        if not reference or not display.endswith(reference):
            super().paint(painter, option, index)
            return

        prefix = display[: len(display) - len(reference)]

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, option.widget)

        from cockpit.ui import facelift

        metrics = opt.fontMetrics
        prefix_width = metrics.horizontalAdvance(prefix)
        total_width = metrics.horizontalAdvance(display)
        cell = option.rect
        left = cell.x() + max(0, (cell.width() - total_width) // 2)

        painter.save()
        painter.setFont(opt.font)
        if prefix:
            painter.setPen(opt.palette.text().color())
            painter.drawText(
                QRect(left, cell.y(), prefix_width, cell.height()),
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                prefix,
            )
        painter.setPen(facelift.attention_color())
        painter.drawText(
            QRect(left + prefix_width, cell.y(), total_width - prefix_width, cell.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            reference,
        )
        painter.restore()


class GroupHeaderDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if row_data and row_data["kind"] == RowKind.GROUP_HEADER:
            if index.column() == 0:
                painter.save()
                font = painter.font()
                font.setBold(True)
                painter.setFont(font)
                painter.fillRect(option.rect, option.palette.alternateBase())
                painter.drawText(option.rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter, str(row_data["label"]))
                painter.restore()
            else:
                painter.fillRect(option.rect, option.palette.alternateBase())
            return
            
        super().paint(painter, option, index)


class ViewNameDialog(QDialog):
    """Asks for a view's name; a refused name is shown here and the dialog stays open (Phase 59 §6.4)."""

    def __init__(
        self,
        title: str,
        initial: str,
        validate: Callable[[str], Result],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._validate = validate
        self.accepted_name: str | None = None
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(initial)
        self.name_edit.setMaxLength(64)
        layout.addWidget(self.name_edit)
        self.error_label = QLabel("")
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        layout.addWidget(self.error_label)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.try_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def try_accept(self) -> None:
        result = self._validate(self.name_edit.text())
        if result.is_err:
            self.error_label.setText(view_name_refusal_text(result.error))
            self.error_label.show()
            return
        self.accepted_name = result.value
        self.accept()


class OpenAuditPicker(QWidget):
    audit_selected = pyqtSignal(int)
    complete_requested = pyqtSignal(int)
    status_change_requested = pyqtSignal(int, AuditStatus)
    ship_date_change_requested = pyqtSignal(int, object)  # (audit_id, date | None)
    ops_per_board_change_requested = pyqtSignal(int, object)  # (audit_id, float | None)
    # Both carry the chosen IngestionMode, which fixes the search scope.
    new_audit_requested = pyqtSignal(object)
    fetch_job_requested = pyqtSignal(object)
    holidays_requested = pyqtSignal()
    font_scale_change_requested = pyqtSignal(int)
    settings_requested = pyqtSignal()
    label_toggle_requested = pyqtSignal(int, bool)
    photos_toggle_requested = pyqtSignal(int, bool)
    xray_toggle_requested = pyqtSignal(int, bool)
    second_ops_requested = pyqtSignal()
    # Phase 59 §4.5: (part number, RequirementKind value, flagged); MainWindow writes it.
    hand_flag_requested = pyqtSignal(str, str, bool)

    def __init__(self, parent: QWidget | None = None, *, settings: QSettings | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        # Phase 59 §6.3: set while a layout is applied, so the header's signals don't save it half done.
        self._applying_layout = False
        layout = QVBoxLayout(self)
        
        layout.addLayout(self.build_title_row())
        
        self.table_view = QTableView()
        self.table_view.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table_view.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table_view.verticalHeader().setVisible(False)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table_view.clicked.connect(self._on_table_clicked)
        self.table_view.setItemDelegate(GroupHeaderDelegate(self.table_view))
        
        self.model = AuditListModel(self)
        self.model.label_toggle_requested.connect(self.label_toggle_requested.emit)
        self.model.photos_toggle_requested.connect(self.photos_toggle_requested.emit)
        self.model.xray_toggle_requested.connect(self.xray_toggle_requested.emit)
        self.table_view.setModel(self.model)

        header = self.table_view.horizontalHeader()
        assert header.count() == len(Column)
        header.setSectionsMovable(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        header.setSortIndicatorShown(False)
        # Phase 59 §6.2: the saved views, the pre-Phase 59 layout carried over once.
        self._views = load_views(settings, COLUMN_SPECS, self._decode_legacy_header)
        self._current_layout: ColumnLayout = self._views.active_view().layout
        self._apply_layout(self._current_layout)
        self._sync_view_combo()
        header.sectionMoved.connect(self._on_section_moved)
        header.sectionResized.connect(self._on_section_resized)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._on_header_context_menu)
        
        for col in (Column.LABEL, Column.PHOTOS, Column.XRAY):
            self.table_view.setItemDelegateForColumn(col, CenteredCheckDelegate(self.table_view))
        self.table_view.setItemDelegateForColumn(
            Column.REPEAT, RepeatReferenceDelegate(self.table_view)
        )
        
        layout.addWidget(self.table_view)
        self._last_digests = []
        
        self._install_context_menu()

    def set_new_audit_unavailable(self, reason: str | None) -> None:
        """Phase 55b §3.2: New audit… is disabled, with the reason, while an operation runs."""
        button = getattr(self, "new_btn", None)
        if button is None:
            return
        button.setEnabled(reason is None)
        button.setToolTip(reason or "")

    def build_title_row(self) -> QHBoxLayout:
        title = QLabel("Select an Audit")
        title.setProperty("class", "h1")
        
        self.new_btn = QPushButton("New audit...")
        from PyQt6.QtWidgets import QMenu
        from cockpit.services.ingestion_mode import IngestionMode

        menu = QMenu(self.new_btn)
        # Mode first, then entry point. Article Revision is Fetch-only: its
        # ordinal comes from a folder name on the share, and a manual drop of
        # loose files carries nothing to read it from.
        self._mode_menus = {}
        for mode in (IngestionMode.STANDARD, IngestionMode.SPLIT):
            submenu = QMenu(mode.label, menu)
            submenu.addAction("Fetch by job number...").triggered.connect(
                lambda _c=False, m=mode: self.fetch_job_requested.emit(m)
            )
            submenu.addAction("Manual upload...").triggered.connect(
                lambda _c=False, m=mode: self.new_audit_requested.emit(m)
            )
            menu.addMenu(submenu)
            self._mode_menus[mode] = submenu

        article = IngestionMode.ARTICLE_REVISION
        menu.addAction(f"{article.label}...").triggered.connect(
            lambda _c=False, m=article: self.fetch_job_requested.emit(m)
        )
        self._new_menu = menu
        self.new_btn.setMenu(menu)
        
        self.holidays_btn = QPushButton("Holidays...")
        self.holidays_btn.clicked.connect(self._on_holidays_clicked)
        
        minus = QPushButton("A-")
        minus.clicked.connect(lambda: self.font_scale_change_requested.emit(-1))
        plus = QPushButton("A+")
        plus.clicked.connect(lambda: self.font_scale_change_requested.emit(1))

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search...")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(220)
        self.search_edit.textChanged.connect(self._on_search_text_changed)

        layout = QHBoxLayout()
        layout.addWidget(title)
        layout.addStretch()
        layout.addWidget(self.new_btn)
        layout.addWidget(self.holidays_btn)
        layout.addWidget(minus)
        layout.addWidget(plus)

        self.second_ops_btn = QPushButton("2nd OPS...")
        self.second_ops_btn.clicked.connect(self.second_ops_requested.emit)
        layout.addWidget(self.second_ops_btn)

        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)

        # Phase 59 §6.4: the saved views; right-click a column header to manage them.
        self.view_label = QLabel("View:")
        self.view_combo = QComboBox()
        self.view_combo.setToolTip("Saved column views; right-click a column header to manage them")
        self.view_combo.activated.connect(self._on_view_activated)
        layout.addWidget(self.view_label)
        layout.addWidget(self.view_combo)

        layout.addWidget(self.search_edit)
        return layout

    def populate(self, digests: Sequence[OpenAuditDigest], *, keep_scroll: bool = False) -> None:
        """Shows digests; keep_scroll holds your place, for refreshes you didn't ask for."""
        scroll_bar = self.table_view.verticalScrollBar()
        scroll_position = scroll_bar.value()
        self._last_digests = list(digests)
        self.table_view.clearSpans()
        self.model.rebuild(self._filtered_digests())
        if keep_scroll:
            scroll_bar.setValue(min(scroll_position, scroll_bar.maximum()))
        else:
            self.table_view.scrollToTop()
        self._apply_group_header_spans()

    def _apply_group_header_spans(self) -> None:
        for i in range(self.model.rowCount()):
            idx = self.model.index(i, 0)
            row_data = idx.data(Qt.ItemDataRole.UserRole)
            if row_data and row_data["kind"] == RowKind.GROUP_HEADER:
                self.table_view.setSpan(i, 0, 1, self.model.columnCount())

    # --- Phase 59 §6: saved column views ---

    @contextmanager
    def _persistence_suspended(self):
        """§6.3: the header's signals don't save while a layout is applied or a column shown."""
        previous = self._applying_layout
        self._applying_layout = True
        try:
            yield
        finally:
            self._applying_layout = previous

    def _decode_legacy_header(self, blob) -> ColumnLayout | None:
        """§6.2: the pre-Phase 59 header state as a layout of today's columns, read on a scratch header.

        post: absent when the scratch header refuses blob; otherwise the restored order, widths and
              hidden flags of the 24 legacy columns, a hidden section's width read by showing it on the
              scratch header (a hidden section reports 0), with COMPANY right after WORK_ORDER_REF,
              visible, at its default width. The Audit List's own header is never touched.
        """
        scratch = QHeaderView(Qt.Orientation.Horizontal)
        scratch.setModel(self.model)
        if not scratch.restoreState(blob) or scratch.count() != len(Column):
            return None
        hidden = {logical for logical in range(scratch.count()) if scratch.isSectionHidden(logical)}
        for logical in hidden:
            scratch.setSectionHidden(logical, False)
        placements: list[ColumnPlacement] = []
        for visual in range(scratch.count()):
            logical = scratch.logicalIndex(visual)
            if logical == Column.COMPANY:
                continue
            placements.append(ColumnPlacement(Column(logical).name, scratch.sectionSize(logical), logical not in hidden))
        after = next(i for i, placement in enumerate(placements) if placement.key == Column.WORK_ORDER_REF.name) + 1
        placements.insert(after, ColumnPlacement(Column.COMPANY.name, _DEFAULT_COLUMN_WIDTHS[Column.COMPANY], True))
        return ColumnLayout(tuple(placements))

    def _apply_layout(self, layout: ColumnLayout) -> None:
        """§6.3: shows a layout in the header without saving any step of it."""
        header = self.table_view.horizontalHeader()
        with self._persistence_suspended():
            for target, placement in enumerate(layout.placements):
                logical = int(Column[placement.key])
                current = header.visualIndex(logical)
                if current != target:
                    header.moveSection(current, target)
            for placement in layout.placements:
                header.resizeSection(int(Column[placement.key]), placement.width_px)
            for placement in layout.placements:
                header.setSectionHidden(int(Column[placement.key]), not placement.visible)
        self._current_layout = layout
        self.table_view.clearSpans()
        self._apply_group_header_spans()

    def _capture_layout(self) -> ColumnLayout:
        """§6.3: the header as a layout; a hidden section keeps the width the view last held for it."""
        header = self.table_view.horizontalHeader()
        placements: list[ColumnPlacement] = []
        for visual in range(header.count()):
            logical = header.logicalIndex(visual)
            column = Column(logical)
            hidden = header.isSectionHidden(logical)
            if hidden:
                prior = self._current_layout.placement(column.name)
                width = prior.width_px if prior is not None else _DEFAULT_COLUMN_WIDTHS[column]
            else:
                width = header.sectionSize(logical)
            placements.append(ColumnPlacement(column.name, width, not hidden))
        return reconcile_layout(placements, COLUMN_SPECS)

    def _persist_active_view(self) -> None:
        """§6.3: saves the header's layout into the selected view (R15-3)."""
        if self._applying_layout:
            return
        layout = self._capture_layout()
        self._current_layout = layout
        self._views = self._views.with_layout(self._views.active, layout)
        store_views(self._settings, self._views)

    def _enforce_start_by_pinned(self) -> None:
        header = self.table_view.horizontalHeader()
        if header.visualIndex(Column.START_BY) != 0:
            with self._persistence_suspended():
                header.moveSection(header.visualIndex(Column.START_BY), 0)

    def _on_section_moved(self, logical: int, old_visual: int, new_visual: int) -> None:
        if self._applying_layout:
            return
        self._enforce_start_by_pinned()
        self._persist_active_view()

    def _on_section_resized(self, logical: int, old_size: int, new_size: int) -> None:
        if not self._applying_layout:
            self._persist_active_view()

    def view_names(self) -> list[str]:
        return self._views.names()

    def active_view_name(self) -> str:
        return self._views.active_view().name

    def _sync_view_combo(self) -> None:
        combo = self.view_combo
        was_blocked = combo.blockSignals(True)
        try:
            combo.clear()
            names = self._views.names()
            combo.addItems(names)
            combo.setCurrentIndex(names.index(self._views.active_view().name))
        finally:
            combo.blockSignals(was_blocked)

    def _on_view_activated(self, index: int) -> None:
        name = self.view_combo.itemText(index)
        if name and name != self._views.active_view().name:
            self.select_view(name)

    def select_view(self, name: str) -> None:
        """§6.3: shows a saved view; a hidden sort column falls back to Start-By ascending."""
        view = self._views.find(name)
        if view is None:
            return
        self._views = self._views.with_active(view.name)
        store_views(self._settings, self._views)
        self._apply_layout(view.layout)
        self._sync_view_combo()
        self._reset_sort_if_hidden()

    def set_column_visible(self, column: Column, visible: bool) -> None:
        """§6.3: shows or hides one column in the selected view, then saves once."""
        column = Column(column)
        if column == Column.START_BY:
            return
        header = self.table_view.horizontalHeader()
        if header.isSectionHidden(column) != visible:
            return
        with self._persistence_suspended():
            if visible:
                placement = self._current_layout.placement(column.name)
                width = placement.width_px if placement is not None else _DEFAULT_COLUMN_WIDTHS[column]
                header.setSectionHidden(column, False)
                header.resizeSection(column, width)
            else:
                header.setSectionHidden(column, True)
        self._persist_active_view()
        if not visible:
            self._reset_sort_if_hidden()

    def save_view_as(self, name: str) -> Result:
        """§6.4: copies the current layout into a new view and selects it."""
        checked = validate_view_name(name, self._views)
        if checked.is_err:
            return checked
        self._views = self._views.added(SavedView(checked.value, self._capture_layout()))
        self._current_layout = self._views.active_view().layout
        store_views(self._settings, self._views)
        self._sync_view_combo()
        return checked

    def rename_view(self, name: str) -> Result:
        """§6.4: renames the selected view."""
        active = self._views.active_view().name
        checked = validate_view_name(name, self._views, renaming=active)
        if checked.is_err:
            return checked
        self._views = self._views.renamed(active, checked.value)
        store_views(self._settings, self._views)
        self._sync_view_combo()
        return checked

    def delete_active_view(self) -> bool:
        """§6.4: deletes the selected view and selects the first that remains; never the last one."""
        if len(self._views.views) <= 1:
            return False
        self._views = self._views.removed(self._views.active_view().name)
        store_views(self._settings, self._views)
        self._apply_layout(self._views.active_view().layout)
        self._sync_view_combo()
        self._reset_sort_if_hidden()
        return True

    def reset_active_view(self) -> None:
        """§6.4: puts the registry default into the selected view; other views are untouched."""
        layout = default_layout(COLUMN_SPECS)
        self._apply_layout(layout)
        self._views = self._views.with_layout(self._views.active, layout)
        store_views(self._settings, self._views)
        self._reset_sort_if_hidden()

    def _apply_sort(self, column: int, ascending: bool) -> None:
        """The one sort routine: the rebuild, then the group-header spans again."""
        self.model.set_sort(column, ascending)
        self.table_view.clearSpans()
        self._apply_group_header_spans()

    def _reset_sort_if_hidden(self) -> None:
        if self.table_view.horizontalHeader().isSectionHidden(self.model._sort_col):
            self._apply_sort(Column.START_BY, True)

    def build_header_menu(self) -> QMenu:
        """§6.4: every column but Start-By, in the view's order, then the view actions."""
        header = self.table_view.horizontalHeader()
        menu = QMenu()
        menu.setToolTipsVisible(True)
        for visual in range(header.count()):
            column = Column(header.logicalIndex(visual))
            if column == Column.START_BY:
                continue
            action = menu.addAction(COLUMN_TITLES[column])
            action.setCheckable(True)
            action.setChecked(not header.isSectionHidden(column))
            action.triggered.connect(
                lambda checked=False, target=column: self.set_column_visible(target, bool(checked))
            )
        menu.addSeparator()
        save_as = menu.addAction("Save as new view…")
        if len(self._views.views) >= MAX_SAVED_VIEWS:
            save_as.setEnabled(False)
            save_as.setToolTip("Nine views is the limit; delete one first")
        else:
            save_as.triggered.connect(self._on_save_view_as)
        rename = menu.addAction("Rename view…")
        rename.triggered.connect(self._on_rename_view)
        delete = menu.addAction("Delete view…")
        if len(self._views.views) <= 1:
            delete.setEnabled(False)
        else:
            delete.triggered.connect(self._on_delete_view)
        menu.addSeparator()
        reset = menu.addAction("Reset view to default columns")
        reset.triggered.connect(self.reset_active_view)
        return menu

    def _on_header_context_menu(self, position) -> None:
        menu = self.build_header_menu()
        menu.exec(self.table_view.horizontalHeader().mapToGlobal(position))

    def _ask_view_name(self, title: str, initial: str, validate: Callable[[str], Result]) -> str | None:
        dialog = ViewNameDialog(title, initial, validate, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.accepted_name
        return None

    def _confirm_delete_view(self, name: str) -> bool:
        answer = QMessageBox.question(
            self, "Delete view", f"Delete view {name}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_save_view_as(self) -> None:
        name = self._ask_view_name("Save as new view", "", lambda text: validate_view_name(text, self._views))
        if name is not None:
            self.save_view_as(name)

    def _on_rename_view(self) -> None:
        active = self._views.active_view().name
        name = self._ask_view_name(
            "Rename view", active, lambda text: validate_view_name(text, self._views, renaming=active),
        )
        if name is not None:
            self.rename_view(name)

    def _on_delete_view(self) -> None:
        if self._confirm_delete_view(self._views.active_view().name):
            self.delete_active_view()

    # --- Global search (Phase 32, 2.3; Phase 54, §9) ---

    def _searchable_text(self, d: OpenAuditDigest) -> str:
        """Concatenation of every List View column, shown or hidden, mirroring the model's display.

        Phase 59 §6.5: a view hiding a column doesn't hide it from search. Company contributes the
        name shown and the traveler's own; a hand flag adds the token a detection adds.
        """
        required = (BadgeState.DETECTED, BadgeState.FLAGGED)
        aq_tok = "aquanox" if _req_badge(d, RequirementKind.AQUANOX) in required else ""
        pf_tok = "pressfit" if _req_badge(d, RequirementKind.PRESS_FIT) in required else ""
        x_st = xray_cell_state(d.requirements, d.xray_tick)
        xray_tokens: list[str] = []
        if x_st in (
            XrayCellState.XR_ONLY,
            XrayCellState.PENDING,
            XrayCellState.DONE,
            XrayCellState.DONE_STALE,
        ):
            xray_tokens.append("xray")
        if x_st == XrayCellState.PENDING:
            xray_tokens.append("xraypending")
        elif x_st == XrayCellState.DONE:
            xray_tokens.append("xraydone")
        elif x_st == XrayCellState.DONE_STALE:
            xray_tokens.append("xraystale")

        parts = [
            _format_list_date(d.start_by),
            _format_list_date(d.ship_date),
            str(d.part_number),
            f"{d.work_order_ref}{d.split_suffix}",
            self.model.company_text(d),
            getattr(d, "customer", ""),
            str(d.lead_time_days) if d.lead_time_days is not None else "",
            str(d.quantity),
            d.repeat.display,
            str(d.classification),
            str(d.assembly_class) if d.assembly_class is not None else "",
            d.process or "",
            # Not the displayed C / NC: "c" would match any cell containing a c,
            # and "clean" is a substring of "noclean". These two share none.
            WASH_SEARCH_TOKEN[wash_state_of(d)],
            aq_tok,
            pf_tok,
            f"{d.feeder_setuptime:.1f}" if d.feeder_setuptime is not None else "",
            f"{d.smt_runtime:.1f}" if d.smt_runtime is not None else "",
            f"{d.tht_runtime:.1f}" if d.tht_runtime is not None else "",
            f"{d.aoi_runtime:.1f}" if d.aoi_runtime is not None else "",
            f"{d.ops_runtime:.1f}" if d.ops_runtime is not None else "",
            f"{d.shipping_runtime:.1f}" if d.shipping_runtime is not None else "",
            f"{d.date_ingested.astimezone(PST):%Y-%m-%d}" if d.date_ingested else "",
            "labeled" if d.is_labeled else "",
            "photos" if d.are_photos_uploaded else "",
            " ".join(xray_tokens),
            _article_text(d),
        ]
        return " ".join(parts).casefold()

    def _filtered_digests(self) -> list:
        needle = self.search_edit.text().strip().casefold() if hasattr(self, "search_edit") else ""
        if not needle:
            return list(self._last_digests)
        return [d for d in self._last_digests if needle in self._searchable_text(d)]

    def _on_search_text_changed(self, _text: str) -> None:
        self.table_view.clearSpans()
        self.model.rebuild(self._filtered_digests())
        self._apply_group_header_spans()

    def hideEvent(self, event) -> None:
        # UX: search resets whenever the user leaves the List View.
        if hasattr(self, "search_edit") and self.search_edit.text():
            self.search_edit.clear()
        super().hideEvent(event)

    def _on_header_clicked(self, logical_index: int) -> None:
        if self.model._sort_col == logical_index:
            self._apply_sort(logical_index, not self.model._sort_asc)
        else:
            self._apply_sort(logical_index, True)

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid():
            return
        if index.column() in (Column.LABEL, Column.PHOTOS):
            return
        if index.column() == Column.XRAY and bool(index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return
            
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if row_data and row_data["kind"] == RowKind.DATA:
            digest = row_data["digest"]
            self.audit_selected.emit(digest.audit_id)
        else:
            self.table_view.clearSelection()

    def _on_holidays_clicked(self) -> None:
        self.holidays_requested.emit()

    def _install_context_menu(self) -> None:
        self.table_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table_view.customContextMenuRequested.connect(self._on_context_menu_requested)

    def _on_context_menu_requested(self, position) -> None:
        index = self.table_view.indexAt(position)
        if not index.isValid():
            return
            
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if not row_data or row_data.get("kind") != RowKind.DATA:
            return
            
        digest: OpenAuditDigest = row_data["digest"]
        menu = self.build_row_menu(digest)
        menu.exec(self.table_view.viewport().mapToGlobal(position))

    def build_row_menu(self, digest: OpenAuditDigest) -> QMenu:
        """The row's menu; Phase 59 §4.5 adds the "Flag for {B#}" submenu at its end."""
        menu = QMenu()
        complete_action = menu.addAction("Complete")
        complete_action.triggered.connect(lambda _, a_id=digest.audit_id: self.complete_requested.emit(a_id))

        status_menu = menu.addMenu("Status")
        for st in AuditStatus.ordered():
            action = status_menu.addAction(st.value)
            action.setCheckable(True)
            if st == digest.status:
                action.setChecked(True)
                action.setEnabled(False)
            else:
                action.triggered.connect(lambda _, a_id=digest.audit_id, status=st: self.status_change_requested.emit(a_id, status))

        ship_date_action = menu.addAction("Ship Date...")
        ship_date_action.triggered.connect(lambda _, d=digest: self._on_ship_date_action(d))

        ops_action = menu.addAction("OPS per board…")
        ops_action.triggered.connect(lambda _, d=digest: self._on_ops_action(d))

        menu.addSeparator()
        flag_menu = menu.addMenu(flag_menu_title(digest.part_number))
        populate_hand_flag_menu(
            flag_menu, digest.part_number, getattr(digest, "hand_flags", None) or {}, digest.requirements,
        )
        flag_menu.triggered.connect(self._on_flag_action_triggered)
        return menu

    def _on_flag_action_triggered(self, action) -> None:
        request = flag_request_of(action)
        if request is not None:
            self.hand_flag_requested.emit(*request)

    def _on_ship_date_action(self, digest: OpenAuditDigest) -> None:
        from cockpit.ui.widgets.dialogs import ShipDateDialog
        dialog = ShipDateDialog(digest.ship_date, self)
        if dialog.exec():
            self.ship_date_change_requested.emit(digest.audit_id, dialog.selected_date())

    def _on_ops_action(self, digest: OpenAuditDigest) -> None:
        from cockpit.ui.widgets.dialogs import OpsPerBoardDialog
        dialog = OpsPerBoardDialog(digest.ops_per_board_min, self)
        if dialog.exec():
            self.ops_per_board_change_requested.emit(digest.audit_id, dialog.result_value())
