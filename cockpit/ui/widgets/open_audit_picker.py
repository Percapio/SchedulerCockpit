"""Open audit picker."""

from datetime import datetime, timezone, timedelta, date
from typing import Sequence, Any, List
from enum import IntEnum
from PyQt6.QtCore import Qt, pyqtSignal, QAbstractTableModel, QModelIndex, QSortFilterProxyModel
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QTableView, QHeaderView, QStyledItemDelegate, QMenu, QLineEdit
)

from cockpit.persistence.types import AuditStatus
from cockpit.services.views import OpenAuditDigest
from cockpit.ui.widgets.toast import Toast  # Phase 3
from cockpit.ui.widgets.holiday_dialog import HolidayDialog

PST = timezone(timedelta(hours=-8))

class Column(IntEnum):
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
    FSU = 10
    SMT = 11
    THT = 12
    AOI = 13
    OPS = 14
    SHIP = 15
    DATE_INGESTED = 16
    LABEL = 17
    PHOTOS = 18

_CHECKBOX_COLUMNS = frozenset({Column.LABEL, Column.PHOTOS})
_STATUS_STAGE_COLUMN = {
    AuditStatus.FSU: Column.FSU,
    AuditStatus.SMT: Column.SMT,
    AuditStatus.THT: Column.THT,
    AuditStatus.AOI: Column.AOI,
    AuditStatus.OPS: Column.OPS,
    AuditStatus.SHIPPING: Column.SHIP,
}

class RowKind:
    DATA = "data"
    GROUP_HEADER = "group_header"

class AuditListModel(QAbstractTableModel):
    label_toggle_requested = pyqtSignal(int, bool)
    photos_toggle_requested = pyqtSignal(int, bool)

    COLUMNS = [
        "Start-By Date", "Ship Date", "B#", "S/O", "LT",
        "QTY", "Type", "Classification", "Class", "Process",
        "FSU (hrs)", "SMT (hrs)", "THT (hrs)", 
        "AOI (hrs)", "OPS (hrs)", "SHIP (hrs)", "Date Ingested",
        "Label", "Photos"
    ]
    
    GROUP_ORDER = AuditStatus.ordered()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._digests: List[OpenAuditDigest] = []
        self._rows: List[dict] = []
        self._sort_col = 0 # Start-By Date default
        self._sort_asc = True

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
        if col == 0: return (d.start_by is not None, d.start_by)
        if col == 1: return (d.ship_date is not None, d.ship_date)
        if col == 2: return str(d.part_number)
        if col == 3: return f"{d.work_order_ref}{d.split_suffix}"
        if col == 4: return (d.lead_time_days is not None, d.lead_time_days)
        if col == 5: return d.quantity
        if col == 6: return str(d.repeat)
        if col == 7: return str(d.classification)
        if col == 8: return (d.assembly_class is not None, d.assembly_class)
        if col == 9: return str(d.process)
        if col == 10: return (d.feeder_setuptime is not None, d.feeder_setuptime)
        if col == 11: return (d.smt_runtime is not None, d.smt_runtime)
        if col == 12: return (d.tht_runtime is not None, d.tht_runtime)
        if col == 13: return (d.aoi_runtime is not None, d.aoi_runtime)
        if col == 14: return (d.ops_runtime is not None, d.ops_runtime)
        if col == 15: return (d.shipping_runtime is not None, d.shipping_runtime)
        if col == 16: return (d.date_ingested is not None, d.date_ingested)
        if col == 17: return d.is_labeled
        if col == 18: return d.are_photos_uploaded
        return 0

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        row_data = self._rows[index.row()]
        if row_data["kind"] == RowKind.GROUP_HEADER:
            return Qt.ItemFlag.ItemIsEnabled
        col = index.column()
        if col in _CHECKBOX_COLUMNS:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
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

        if role == Qt.ItemDataRole.CheckStateRole:
            if row_data["kind"] == RowKind.DATA and index.column() in _CHECKBOX_COLUMNS:
                d: OpenAuditDigest = row_data["digest"]
                val = d.is_labeled if index.column() == Column.LABEL else d.are_photos_uploaded
                return Qt.CheckState.Checked if val else Qt.CheckState.Unchecked
            return None

        # Phase 32 (2.5): semantic highlighting for B#, QTY, Process columns.
        if role == Qt.ItemDataRole.ForegroundRole and row_data["kind"] == RowKind.DATA:
            from cockpit.ui import facelift
            d: OpenAuditDigest = row_data["digest"]
            if index.column() == _STATUS_STAGE_COLUMN.get(d.status):
                return facelift.attention_color()
            if index.column() == 7 and d.is_itar:
                return facelift.attention_color()
            
            color = facelift.list_column_color(index.column())
            if color is not None:
                return color

        if role == Qt.ItemDataRole.DisplayRole:
            if index.column() in _CHECKBOX_COLUMNS:
                return None
            d: OpenAuditDigest = row_data["digest"]
            col = index.column()
            
            if col == 0: return str(d.start_by) if d.start_by else ""
            if col == 1: return str(d.ship_date) if d.ship_date else ""
            if col == 2: return d.part_number
            if col == 3: return f"{d.work_order_ref}{d.split_suffix}"
            if col == 4: return str(d.lead_time_days) if d.lead_time_days is not None else ""
            if col == 5: return str(d.quantity)
            if col == 6: return d.repeat
            if col == 7: return d.classification
            if col == 8: return str(d.assembly_class) if d.assembly_class is not None else ""
            if col == 9: return d.process or ""
            if col == 10: return f"{d.feeder_setuptime:.1f}" if d.feeder_setuptime is not None else ""
            if col == 11: return f"{d.smt_runtime:.1f}" if d.smt_runtime is not None else ""
            if col == 12: return f"{d.tht_runtime:.1f}" if d.tht_runtime is not None else ""
            if col == 13: return f"{d.aoi_runtime:.1f}" if d.aoi_runtime is not None else ""
            if col == 14: return f"{d.ops_runtime:.1f}" if d.ops_runtime is not None else ""
            if col == 15: return f"{d.shipping_runtime:.1f}" if d.shipping_runtime is not None else ""
            if col == 16:
                if not d.date_ingested: return ""
                local = d.date_ingested.astimezone(PST)
                return f"{local:%Y-%m-%d}"

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
            else:
                self.photos_toggle_requested.emit(d.audit_id, checked)
            return True
        return super().setData(index, value, role)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal:
            if role == Qt.ItemDataRole.DisplayRole:
                return self.COLUMNS[section]
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
        return None


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


class OpenAuditPicker(QWidget):
    audit_selected = pyqtSignal(int)
    complete_requested = pyqtSignal(int)
    status_change_requested = pyqtSignal(int, AuditStatus)
    ship_date_change_requested = pyqtSignal(int, object)  # (audit_id, date | None)
    ops_per_board_change_requested = pyqtSignal(int, object)  # (audit_id, float | None)
    new_audit_requested = pyqtSignal()
    font_scale_change_requested = pyqtSignal(int)
    settings_requested = pyqtSignal()
    label_toggle_requested = pyqtSignal(int, bool)
    photos_toggle_requested = pyqtSignal(int, bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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
        self.table_view.setModel(self.model)
        
        layout.addWidget(self.table_view)
        self._last_digests = []
        
        self._install_context_menu()

    def build_title_row(self) -> QHBoxLayout:
        title = QLabel("Select an Audit")
        title.setProperty("class", "h1")
        
        self.new_btn = QPushButton("+ New audit")
        self.new_btn.clicked.connect(self.new_audit_requested.emit)
        
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

        self.settings_btn = QPushButton("Settings...")
        self.settings_btn.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self.settings_btn)

        layout.addWidget(self.search_edit)
        return layout

    def populate(self, digests: Sequence[OpenAuditDigest]) -> None:
        self._last_digests = list(digests)
        self.table_view.clearSpans()
        self.model.rebuild(self._filtered_digests())
        self.table_view.scrollToTop()
        self._apply_group_header_spans()

    def _apply_group_header_spans(self) -> None:
        for i in range(self.model.rowCount()):
            idx = self.model.index(i, 0)
            row_data = idx.data(Qt.ItemDataRole.UserRole)
            if row_data and row_data["kind"] == RowKind.GROUP_HEADER:
                self.table_view.setSpan(i, 0, 1, self.model.columnCount())

    # --- Global search (Phase 32, 2.3) ---

    def _searchable_text(self, d: OpenAuditDigest) -> str:
        """Concatenation of every visible List View column, mirroring the model's display."""
        parts = [
            str(d.start_by) if d.start_by else "",
            str(d.ship_date) if d.ship_date else "",
            str(d.part_number),
            f"{d.work_order_ref}{d.split_suffix}",
            str(d.lead_time_days) if d.lead_time_days is not None else "",
            str(d.quantity),
            str(d.repeat),
            str(d.classification),
            str(d.assembly_class) if d.assembly_class is not None else "",
            d.process or "",
            f"{d.feeder_setuptime:.1f}" if d.feeder_setuptime is not None else "",
            f"{d.smt_runtime:.1f}" if d.smt_runtime is not None else "",
            f"{d.tht_runtime:.1f}" if d.tht_runtime is not None else "",
            f"{d.aoi_runtime:.1f}" if d.aoi_runtime is not None else "",
            f"{d.ops_runtime:.1f}" if d.ops_runtime is not None else "",
            f"{d.shipping_runtime:.1f}" if d.shipping_runtime is not None else "",
            f"{d.date_ingested.astimezone(PST):%Y-%m-%d}" if d.date_ingested else "",
            "labeled" if d.is_labeled else "",
            "photos" if d.are_photos_uploaded else "",
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
            self.model.set_sort(logical_index, not self.model._sort_asc)
        else:
            self.model.set_sort(logical_index, True)

        self.table_view.clearSpans()
        self._apply_group_header_spans()

    def _on_table_clicked(self, index: QModelIndex) -> None:
        if not index.isValid() or index.column() in _CHECKBOX_COLUMNS:
            return
            
        row_data = index.data(Qt.ItemDataRole.UserRole)
        if row_data and row_data["kind"] == RowKind.DATA:
            digest = row_data["digest"]
            self.audit_selected.emit(digest.audit_id)
        else:
            self.table_view.clearSelection()

    def _on_holidays_clicked(self) -> None:
        # We need access to holiday_svc. We can get it from the app or passed in.
        app_win = self.window()
        if hasattr(app_win, "_holiday_svc") and hasattr(app_win, "_reload_list"):
            dialog = HolidayDialog(app_win._holiday_svc, self)
            dialog.exec()
            app_win._reload_list()
        else:
            logger.error("Holidays: holiday service unavailable on top-level window %r", app_win)

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

        menu.exec(self.table_view.viewport().mapToGlobal(position))

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
