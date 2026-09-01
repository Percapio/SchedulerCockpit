from typing import Any
import pathlib

from PyQt6.QtCore import Qt, QThread, QModelIndex, QAbstractTableModel, QObject
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QTreeWidget, QTreeWidgetItem, QMenu, QTableView,
    QLabel, QPushButton, QHBoxLayout, QWidget, QAbstractItemView,
    QApplication, QMessageBox, QCheckBox
)
from PyQt6.QtGui import QAction

from ...services.second_ops import (
    list_candidates_for_open_audits, resolve_bom_workbook, render_tsv,
    SecondOpsSettingsController, ReadFailureCause, SecondOpsRow
)
from ...persistence.repositories.bom_components import AuditBomComponentRepository
from ...persistence.repositories.source_files import SourceFileRepository
from ...ingestion.hashing import sha256_hex, HashingError
from ..workers.second_ops_worker import SecondOpsReadWorker
from .open_audit_picker import CenteredCheckDelegate, RowKind
from ...ingestion.parsers.audit_bom import CANONICAL_COLUMNS, RawBomRow


# A QThread with no Qt parent is owned by its Python wrapper. Once the dialog
# that started a read is destroyed, nothing else holds the thread, and a
# garbage-collected QThread that is still running aborts the process. Reads
# outlive the dialog by design (openpyxl is not interruptible), so the thread
# is anchored here for the duration and released on `finished`.
_READS_IN_FLIGHT: set[QThread] = set()


def _is_checked(value: Any) -> bool:
    """True for every representation of Qt.CheckState.Checked a view may pass.

    Qt's own views hand `setData` an int; `CenteredCheckDelegate` hands it the
    `Qt.CheckState` enum member. In PyQt6 the two do not compare equal, so a
    single-form test silently reads every tick as Unchecked.
    """
    return value in (Qt.CheckState.Checked, Qt.CheckState.Checked.value, 2, True)


class SecondOpsOverviewDialog(QDialog):
    def __init__(
        self,
        bom_repo: AuditBomComponentRepository,
        settings_controller: SecondOpsSettingsController,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("2nd OPS Candidates")
        self.resize(800, 600)
        self.setModal(True)

        self._chosen_audit_id: int | None = None
        self.tree: QTreeWidget | None = None

        layout = QVBoxLayout(self)

        terms = settings_controller.terms()
        if not terms:
            self._add_message(layout, "No 2nd OPS terms are configured. Check Settings.")
            self._add_close_button(layout)
            return

        candidates = list_candidates_for_open_audits(bom_repo, terms)

        if not candidates:
            self._add_message(
                layout, "No open audit has a line matching the configured terms."
            )
            self._add_close_button(layout)
            return

        info = QLabel(
            "Showing candidate lines from indexed data. Opening an audit reads its "
            "workbook and may surface more lines."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Identity", "Lines / Details"])
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)

        for audit_c in candidates:
            audit_item = QTreeWidgetItem([
                f"{audit_c.part_number} (WO: {audit_c.work_order_ref})",
                f"{len(audit_c.candidates)} candidate lines"
            ])
            audit_item.setData(0, Qt.ItemDataRole.UserRole, audit_c.audit_id)

            for line in audit_c.candidates:
                child = QTreeWidgetItem([
                    f"Find {line.find_number}: {line.component_mpn}",
                    line.description or ""
                ])
                audit_item.addChild(child)

            self.tree.addTopLevelItem(audit_item)

        layout.addWidget(self.tree)
        self.tree.expandAll()
        self._add_close_button(layout)

    def _add_message(self, layout: QVBoxLayout, text: str) -> None:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        layout.addWidget(lbl, stretch=1)

    def _add_close_button(self, layout: QVBoxLayout) -> None:
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)

    def _audit_id_for_item(self, item: QTreeWidgetItem | None) -> int | None:
        if item is None:
            return None
        if item.parent() is not None:
            item = item.parent()
        return item.data(0, Qt.ItemDataRole.UserRole)

    def _on_context_menu(self, pos) -> None:
        if self.tree is None:
            return
        audit_id = self._audit_id_for_item(self.tree.itemAt(pos))
        if audit_id is None:
            return

        menu = QMenu(self)
        open_action = QAction("Open audit", self)
        open_action.triggered.connect(lambda: self._select_and_accept(audit_id))
        menu.addAction(open_action)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        audit_id = self._audit_id_for_item(item)
        if audit_id is not None:
            self._select_and_accept(audit_id)

    def _select_and_accept(self, audit_id: int) -> None:
        self._chosen_audit_id = audit_id
        self.accept()

    def chosen_audit_id(self) -> int | None:
        return self._chosen_audit_id


class SecondOpsTableModel(QAbstractTableModel):
    """Tick column plus the fourteen canonical BOM cells, in sheet order.

    `_visible` holds indices into `_all_rows`, never row objects: two BOM rows
    with identical cells compare equal under the frozen dataclass, so an
    object lookup can resolve to the wrong tick.
    """

    def __init__(self, rows: list[SecondOpsRow], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._all_rows: list[SecondOpsRow] = list(rows)
        self._ticks: list[bool] = [r.is_match for r in self._all_rows]
        self._show_all = False
        self._visible: list[int] = [i for i, r in enumerate(self._all_rows) if r.is_match]

    def set_show_all(self, show_all: bool) -> None:
        self.beginResetModel()
        self._show_all = show_all
        if show_all:
            self._visible = list(range(len(self._all_rows)))
        else:
            self._visible = [i for i, ticked in enumerate(self._ticks) if ticked]
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._visible)

    def columnCount(self, parent=QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(CANONICAL_COLUMNS) + 1

    def headerData(self, section: int, orientation: Qt.Orientation,
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return "" if section == 0 else CANONICAL_COLUMNS[section - 1]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None

        source_idx = self._visible[index.row()]
        col_idx = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col_idx == 0:
                return None
            return self._all_rows[source_idx].row.cells[col_idx - 1]

        if role == Qt.ItemDataRole.CheckStateRole and col_idx == 0:
            return Qt.CheckState.Checked if self._ticks[source_idx] else Qt.CheckState.Unchecked

        if role == Qt.ItemDataRole.UserRole:
            return {"kind": RowKind.DATA}

        return None

    def setData(self, index: QModelIndex, value: Any,
                role: int = Qt.ItemDataRole.EditRole) -> bool:
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole or index.column() != 0:
            return False

        row_idx = index.row()
        source_idx = self._visible[row_idx]
        self._ticks[source_idx] = _is_checked(value)

        if not self._ticks[source_idx] and not self._show_all:
            # Filtered view is "ticked rows only": unticking removes the row.
            self.beginRemoveRows(QModelIndex(), row_idx, row_idx)
            self._visible.pop(row_idx)
            self.endRemoveRows()
        else:
            cell = self.index(row_idx, 0)
            self.dataChanged.emit(cell, cell, [Qt.ItemDataRole.CheckStateRole])

        return True

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        flags = super().flags(index)
        if index.column() == 0:
            flags |= Qt.ItemFlag.ItemIsUserCheckable
        return flags

    def raw_row_at(self, view_row: int) -> RawBomRow:
        return self._all_rows[self._visible[view_row]].row

    def ticked_raw_rows(self) -> list[RawBomRow]:
        return [r.row for i, r in enumerate(self._all_rows) if self._ticks[i]]

    def visible_raw_rows(self) -> list[RawBomRow]:
        return [self._all_rows[i].row for i in self._visible]

    def column_population_basis(self) -> list[RawBomRow]:
        """Rows deciding which canonical columns are worth showing.

        The term-matched set, not the live tick set: basing it on ticks would
        make columns appear and disappear as the operator works.
        """
        if self._show_all:
            return [r.row for r in self._all_rows]
        return [r.row for r in self._all_rows if r.is_match]


class BomDropTarget(QLabel):
    """Drop target for the fallback path.

    A subclass, not event handlers assigned onto a plain QLabel instance: sip
    resolves virtual reimplementations through the type, so an assigned
    `dropEvent` is never called and the drop silently does nothing.
    """

    def __init__(self, on_file_dropped, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_file_dropped = on_file_dropped
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(True)

    def _single_local_path(self, mime) -> pathlib.Path | None:
        if not mime.hasUrls():
            return None
        local = [u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
        if len(local) != 1 or not local[0]:
            return None
        return pathlib.Path(local[0])

    def dragEnterEvent(self, event) -> None:
        if self._single_local_path(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._single_local_path(event.mimeData()) is not None:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        path = self._single_local_path(event.mimeData())
        if path is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self._on_file_dropped(path)


class SecondOpsAuditDialog(QDialog):
    def __init__(
        self,
        audit_id: int,
        source_file_repo: SourceFileRepository,
        settings_controller: SecondOpsSettingsController,
        parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("2nd OPS Review")
        self.resize(1000, 600)
        self.setModal(True)

        self._audit_id = audit_id
        self._source_file_repo = source_file_repo
        self._settings_controller = settings_controller
        self._terms = settings_controller.terms()
        self._worker: SecondOpsReadWorker | None = None
        self._thread: QThread | None = None
        self._expected_hash: str | None = None
        self._model: SecondOpsTableModel | None = None

        layout = QVBoxLayout(self)

        if not self._terms:
            lbl = QLabel("No 2nd OPS terms are configured. Check Settings.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(lbl, stretch=1)
            self._build_button_row(layout)
            return

        self.unverified_banner = QLabel(
            "These rows came from a file that does not match the workbook "
            "ingested for this job."
        )
        self.unverified_banner.setWordWrap(True)
        self.unverified_banner.setStyleSheet(
            "background-color: #f7c948; color: #1a1a1a; font-weight: bold; padding: 4px;"
        )
        self.unverified_banner.hide()
        layout.addWidget(self.unverified_banner)

        self.header_label = QLabel("Reading workbook...")
        layout.addWidget(self.header_label)

        self.show_all_cb = QCheckBox("Show all rows")
        self.show_all_cb.toggled.connect(self._on_show_all_toggled)
        self.show_all_cb.setEnabled(False)
        layout.addWidget(self.show_all_cb)

        self.status_line = QLabel("")
        layout.addWidget(self.status_line)

        self.table = QTableView()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_table_context_menu)
        self.table.setItemDelegateForColumn(0, CenteredCheckDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.hide()
        layout.addWidget(self.table, stretch=1)

        self.drop_area = BomDropTarget(self._on_file_dropped)
        self.drop_area.hide()
        layout.addWidget(self.drop_area, stretch=1)

        self._build_button_row(layout)

        # The hash is captured whatever the resolve outcome: FILE_MISSING still
        # has a known-good hash to check a replacement against.
        self._expected_hash = self._stored_bom_hash()

        resolved = resolve_bom_workbook(self._audit_id, self._source_file_repo)
        if isinstance(resolved, ReadFailureCause):
            self._show_fallback(resolved)
        else:
            self._start_worker(resolved)

    def _build_button_row(self, layout: QVBoxLayout) -> None:
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.close_btn)
        layout.addLayout(btn_layout)

    def _stored_bom_hash(self) -> str | None:
        try:
            files = self._source_file_repo.list_for_audit(self._audit_id)
        except Exception:
            return None
        bom_files = [f for f in files if f.file_category == "BOM"]
        return bom_files[0].file_hash if bom_files else None

    # ---- worker lifecycle -------------------------------------------------

    def _start_worker(self, path: pathlib.Path) -> None:
        self._detach_worker()
        self.header_label.setText("Reading workbook...")
        self.table.hide()
        self.drop_area.hide()

        thread = QThread()
        worker = SecondOpsReadWorker(path, self._terms)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.rows_ready.connect(self._on_rows_ready)
        worker.read_failed.connect(self._on_read_failed)

        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        # Anchors the thread past the dialog's own lifetime; see _READS_IN_FLIGHT.
        _READS_IN_FLIGHT.add(thread)
        thread.finished.connect(lambda t=thread: _READS_IN_FLIGHT.discard(t))

        self._thread = thread
        self._worker = worker
        thread.start()

    def _detach_worker(self) -> None:
        """Drops every link to the in-flight read. Never raises.

        Both terminal paths and `reject()` route through here, so a completed
        read leaves no deleted QThread wrapper behind for a later Close to
        touch -- the RuntimeError that used to leave the dialog unclosable.
        """
        worker, thread = self._worker, self._thread
        self._worker = None
        self._thread = None

        if worker is not None:
            for signal in (worker.rows_ready, worker.read_failed):
                try:
                    signal.disconnect()
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

    def _on_rows_ready(self, rows: list[SecondOpsRow]) -> None:
        self._detach_worker()

        self._model = SecondOpsTableModel(rows)
        self.table.setModel(self._model)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)
        self.drop_area.hide()
        self.table.show()
        self.show_all_cb.setEnabled(True)
        self._apply_column_visibility()

        matched_count = sum(1 for r in rows if r.is_match)
        total_count = len(rows)
        if total_count == 0:
            self.header_label.setText("This workbook has no data rows.")
        else:
            self.header_label.setText(
                f"{matched_count} matched rows of {total_count} - tick to select"
            )

    def _on_read_failed(self, cause: ReadFailureCause) -> None:
        self._detach_worker()
        self._show_fallback(cause)

    # ---- fallback drop ----------------------------------------------------

    def _show_fallback(self, cause: ReadFailureCause) -> None:
        self.table.hide()
        self.show_all_cb.setEnabled(False)
        self.drop_area.show()
        self.header_label.setText("The stored Audit BOM is unavailable")

        reasons = {
            ReadFailureCause.NO_BOM_SOURCE_FILE: "No BOM was ingested for this audit.",
            ReadFailureCause.FILE_MISSING: "The ingested BOM file is missing from storage.",
            ReadFailureCause.UNREADABLE: "The stored BOM could not be read.",
        }
        self.drop_area.setText(
            f"{reasons.get(cause, 'The stored BOM is unavailable.')}\n\n"
            "Drop the .xlsx Audit BOM here to review it."
        )

    def _on_file_dropped(self, path: pathlib.Path) -> None:
        if path.suffix.lower() != ".xlsx":
            QMessageBox.warning(
                self, "Not an Audit BOM workbook",
                "Only an .xlsx Audit BOM workbook can be read here."
            )
            return

        try:
            dropped_hash = sha256_hex(path)
        except HashingError:
            QMessageBox.warning(
                self, "File could not be read",
                "The file could not be read. It may be open in Excel."
            )
            return

        if self._expected_hash and dropped_hash != self._expected_hash:
            response = QMessageBox.warning(
                self, "Unverified workbook",
                "This is not the workbook ingested for this job.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Apply,
                QMessageBox.StandardButton.Cancel
            )
            if response != QMessageBox.StandardButton.Apply:
                return
            self.unverified_banner.show()

        self._start_worker(path)

    # ---- view -------------------------------------------------------------

    def _apply_column_visibility(self) -> None:
        """Hides canonical columns with no content in the rows on show.

        Legacy workbooks carry none of the three optional columns, and a
        matched hardware row typically leaves half the rest blank.
        """
        if self._model is None:
            return
        basis = self._model.column_population_basis()
        self.table.setColumnHidden(0, False)
        for col in range(len(CANONICAL_COLUMNS)):
            populated = any(row.cells[col].strip() for row in basis) if basis else True
            self.table.setColumnHidden(col + 1, not populated)
        self.table.resizeColumnsToContents()

    def _on_show_all_toggled(self, checked: bool) -> None:
        if self._model is None:
            return
        self._model.set_show_all(checked)
        self._apply_column_visibility()

    def _on_selection_changed(self, *_args) -> None:
        self.status_line.setText("")

    def keyPressEvent(self, event) -> None:
        if (event.modifiers() == Qt.KeyboardModifier.ControlModifier
                and event.key() == Qt.Key.Key_C):
            self._copy_selection()
            return
        super().keyPressEvent(event)

    def _copy_selection(self) -> None:
        if self._model is None or not self.table.isVisible():
            return
        selection = self.table.selectionModel().selectedRows()
        if not selection:
            self.status_line.setText("Nothing selected.")
            return
        ordered = sorted(selection, key=lambda idx: idx.row())
        self._copy_to_clipboard([self._model.raw_row_at(idx.row()) for idx in ordered])

    def _copy_ticked(self) -> None:
        if self._model is None:
            return
        rows = self._model.ticked_raw_rows()
        if not rows:
            self.status_line.setText("No rows ticked.")
            return
        self._copy_to_clipboard(rows)

    def _copy_all_shown(self) -> None:
        if self._model is None:
            return
        rows = self._model.visible_raw_rows()
        if not rows:
            self.status_line.setText("No rows shown.")
            return
        self._copy_to_clipboard(rows)

    def _copy_to_clipboard(self, rows: list[RawBomRow]) -> None:
        QApplication.clipboard().setText(render_tsv(rows))
        plural = "row" if len(rows) == 1 else "rows"
        self.status_line.setText(f"Copied {len(rows)} {plural}")

    def _on_table_context_menu(self, pos) -> None:
        if self._model is None:
            return

        menu = QMenu(self)
        idx = self.table.indexAt(pos)
        if idx.isValid():
            view_row = idx.row()
            copy_row_action = QAction("Copy row", self)
            copy_row_action.triggered.connect(
                lambda: self._copy_to_clipboard([self._model.raw_row_at(view_row)])
            )
            menu.addAction(copy_row_action)

        copy_ticked_action = QAction("Copy ticked rows", self)
        copy_ticked_action.triggered.connect(self._copy_ticked)
        menu.addAction(copy_ticked_action)

        copy_shown_action = QAction("Copy all shown rows", self)
        copy_shown_action.triggered.connect(self._copy_all_shown)
        menu.addAction(copy_shown_action)

        menu.exec(self.table.viewport().mapToGlobal(pos))
