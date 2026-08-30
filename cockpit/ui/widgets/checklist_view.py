"""Checklist view widget."""

from PyQt6.QtCore import pyqtSignal, QEvent, Qt, QObject
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QScrollArea, QLabel

from cockpit.services.views import ActiveAuditView, ChecklistRowView, ChecklistRowKey
from cockpit.ui.widgets.component_row import ComponentRowCore
from .qt_lifecycle import purge_widget_subtree, _drain_layout_widgets


class ChecklistView(QScrollArea):
    empty_space_clicked = pyqtSignal()
    body_clicked = pyqtSignal(object)
    mpn_clicked = pyqtSignal(object)

    def __init__(self, theme: 'Theme', parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self.setWidgetResizable(True)
        
        self._container = QWidget()
        self._layout = QVBoxLayout(self._container)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(3)  # From theme.json checklist_panel.row.gutter_px
        self.setWidget(self._container)
        
        self._index: dict[ChecklistRowKey, tuple[ChecklistRowView, ComponentRowCore]] = {}
        
        self.viewport().installEventFilter(self)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if (obj is self.viewport()
                and event.type() == QEvent.Type.MouseButtonPress
                and event.button() == Qt.MouseButton.LeftButton):
            pos_in_container = self._container.mapFrom(
                self.viewport(), event.position().toPoint())
            last_row = self._last_row_widget_or_none()
            if last_row is None or pos_in_container.y() > last_row.geometry().bottom():
                self.empty_space_clicked.emit()
                return True
        return super().eventFilter(obj, event)

    def _last_row_widget_or_none(self) -> ComponentRowCore | None:
        for i in reversed(range(self._layout.count())):
            item = self._layout.itemAt(i)
            widget = item.widget()
            if isinstance(widget, ComponentRowCore):
                return widget
        return None

    def unload(self) -> None:
        prior_children = _drain_layout_widgets(self._layout)
        for child in prior_children:
            purge_widget_subtree(child)
        self._index.clear()

    def is_loaded(self) -> bool:
        return bool(self._index)

    def populate_section(self, views: list[ChecklistRowView], header_text: str) -> None:
        self.unload()
        
        header = QLabel(header_text)
        header.setProperty("class", "section-header")
        self._layout.addWidget(header)
        
        for row_view in views:
            from cockpit.ui.widgets.component_row import ComponentRowFields
            fields = ComponentRowFields(
                find_number=row_view.find_number,
                mpn=row_view.primary_label,
                description=row_view.secondary_label,
                ref_des_list=row_view.ref_des_list
            )
            core = ComponentRowCore(
                view=fields,
                theme=self._theme
            )
            core.setProperty("class", "component-card checklist-row")
            core.refdes_chip_clicked.connect(lambda _, k=row_view.key: self.body_clicked.emit(k))
            core.mpn_label_clicked.connect(lambda _, k=row_view.key: self.mpn_clicked.emit(k))
            self._layout.addWidget(core)
            self._index[row_view.key] = (row_view, core)
            
        self._layout.addStretch()

    def set_selected_row(self, row_key: ChecklistRowKey) -> None:
        for k, (_, core) in self._index.items():
            core.set_mpn_selected(k == row_key)

    def clear_selected_row(self) -> None:
        for _, core in self._index.values():
            core.set_mpn_selected(False)

    def scroll_to_row(self, row_key: ChecklistRowKey) -> None:
        if row_key in self._index:
            self.ensureWidgetVisible(self._index[row_key][1])

    def apply_filter(self, query: str) -> None:
        q = query.strip().lower()
        vbar = self.verticalScrollBar()
        old_val = vbar.value()
        
        for view, core in self._index.values():
            if not q:
                core.setVisible(True)
                continue
            
            text = view.primary_label.lower()
            if view.secondary_label:
                text += " " + view.secondary_label.lower()
            if view.ref_des_list:
                text += " " + " ".join(view.ref_des_list).lower()
                
            core.setVisible(q in text)
            
        vbar.setValue(old_val)
