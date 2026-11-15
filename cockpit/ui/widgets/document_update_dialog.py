"""The document-update dialog: the preview, its pickers, and the results (Phase 55b §7).

The text of each row and of the details pane is built by module-level functions, so it is tested
without a window. Every text is plain: a file name or field value holding "<b>" shows as typed.
"""

from __future__ import annotations

import pathlib
from datetime import datetime
from typing import Any, Iterable, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cockpit.ingestion.roles import SourceRole
from cockpit.ingestion.version_tags import Dated, version_tag
from cockpit.services import document_updates as du
from cockpit.services import placement_updates as pu
from cockpit.services import update_run as ur
from cockpit.placements.wording import reason_text

ROLE_WORDS: dict[SourceRole, str] = {
    SourceRole.BOM: "BOM",
    SourceRole.NOTES: "ECO",
    SourceRole.TRAVELER: "traveler",
    SourceRole.PLACEMENTS: "placements",
}
DOCUMENT_ROLES = (SourceRole.BOM, SourceRole.NOTES, SourceRole.TRAVELER)
KEPT_UNCHANGED = "Kept unchanged: status, ship date, notes, Label, Photos, X-ray tick, OPS/board, quantity"
REQUIREMENT_LABELS = (
    ("aquanox", "AQ"), ("press_fit", "PF"), ("xray", "XR"), ("xray_save_images", "save images"),
)
STATE_WORDS = {"detected": "yes", "absent": "no", "unknown": "?"}

COLUMN_TICK, COLUMN_JOB, COLUMN_CHANGED, COLUMN_OUTCOME = range(4)


# ---------- words ----------

def _role(role: SourceRole) -> str:
    return ROLE_WORDS.get(role, role.value)


def _when(ns: Optional[int]) -> str:
    if not ns:
        return "unknown"
    return datetime.fromtimestamp(ns / 1e9).strftime("%d %b %Y %H:%M")


def _tag(name: str) -> str:
    tag = version_tag(name)
    return f"dated {tag.date:%d %b %Y}" if isinstance(tag, Dated) else "undated"


def job_text(digest: ur.ProbeDigest) -> str:
    text = digest.key.part_number
    suffixes = [s for s in digest.suffixes if s]
    if suffixes:
        text += f" ({', '.join(suffixes)})"
    if (digest.article_revision or "FA").strip().upper() != "FA":
        text += f", {digest.article_revision} article"
    return text


def _failure_text(failure: Any) -> str:
    if isinstance(failure, du.EscapesRoot):
        return "its folder is outside the Source Root"
    if isinstance(failure, du.ArticleFolderAmbiguous):
        return "two folders claim this article"
    if isinstance(failure, du.CandidateUnreadable):
        return "a file can't be read"
    if isinstance(failure, du.ShareUnreachable):
        return "the share couldn't be reached"
    return type(failure).__name__


def _picked_held(record: ur.FamilyRecord, role: SourceRole) -> bool:
    preview = record.preview
    if preview is None or role not in record.picked:
        return False
    if role == SourceRole.PLACEMENTS:
        return role not in preview.updates and preview.placements_unavailable is None
    return role not in preview.updates and role not in preview.rejected and role not in preview.awaiting_choice


def what_changed(record: ur.FamilyRecord) -> str:
    """The row's "What changed" (§7.2): statuses joined by semicolons, same statuses sharing a phrase."""
    digest = record.digest
    if digest is None:
        return "not checked"
    if digest.failure is not None:
        return _failure_text(digest.failure)
    preview = record.preview
    groups: dict[str, list[str]] = {}
    phrases: list[str] = []
    for role in DOCUMENT_ROLES:
        status = digest.statuses.get(role)
        if preview is not None and role in preview.rejected:
            rejection = preview.rejected[role]
            if isinstance(rejection, du.IdentityMismatch):
                phrases.append(f"{_role(role)} is for another job or order")
            else:
                groups.setdefault("rejected", []).append(_role(role))
            continue
        if _picked_held(record, role):
            phrases.append(f"{_role(role)}: your pick is the copy Cockpit holds")
            continue
        if isinstance(status, du.Changed) or (preview is not None and role in preview.updates
                                               and preview.updates[role].older_reason is None):
            groups.setdefault("changed", []).append(_role(role))
        elif isinstance(status, du.OlderOnShare):
            groups.setdefault("older on share", []).append(_role(role))
        elif isinstance(status, du.NeedsChoice):
            phrases.append(f"{_role(role)}: pick one of {len(status.candidates)}")
    for words in ("changed", "older on share", "rejected"):
        if words in groups:
            phrases.insert(0 if words == "changed" else len(phrases), f"{', '.join(groups[words])} {words}")
    placements = digest.placements
    if _picked_held(record, SourceRole.PLACEMENTS):
        phrases.append("placements: your pick is the file Cockpit holds")
    elif isinstance(placements, pu.PlacementsChanged) or (preview is not None and SourceRole.PLACEMENTS in preview.updates):
        phrases.append("placements: changed file" if digest.placements_held else "placements: new file")
    elif isinstance(placements, pu.PlacementsNeedChoice):
        phrases.append("placements: pick a .bmb")
    elif isinstance(placements, du.ShareReadFailure) or (preview is not None and preview.placements_unavailable):
        phrases.append("placements unavailable")
    if phrases:
        return "; ".join(phrases)
    if any(isinstance(s, du.Missing) for s in digest.statuses.values()):
        return "no job folder"
    not_stored = [_role(r) for r in DOCUMENT_ROLES if isinstance(digest.statuses.get(r), du.NotStored)]
    if not_stored:
        return f"{', '.join(not_stored)} not stored"
    return "unchanged"


def is_listed(record: ur.FamilyRecord) -> bool:
    """§7.2: listed when there is something to act on or look at; the rest wait for Show all."""
    digest = record.digest
    if digest is None:
        return False
    if digest.failure is not None:
        return not isinstance(digest.failure, du.Missing)
    return digest.to_compare


def hidden_kind(record: ur.FamilyRecord) -> str:
    text = what_changed(record)
    if text == "no job folder":
        return "without a job folder"
    if text.endswith("not stored"):
        return "not stored"
    return "unchanged"


def hidden_line(records: Iterable[ur.FamilyRecord]) -> str:
    counts: dict[str, int] = {}
    for record in records:
        if record.digest is not None and not is_listed(record):
            kind = hidden_kind(record)
            counts[kind] = counts.get(kind, 0) + 1
    order = ("unchanged", "without a job folder", "not stored")
    return ", ".join(f"{counts[k]:,} {k}" for k in order if counts.get(k))


def outcome_text(record: ur.FamilyRecord) -> str:
    """§7.5's outcome column."""
    outcome = record.outcome
    if outcome is None:
        return ""
    if isinstance(outcome, ur.Applied):
        warned = outcome.updated.warnings or any(
            not isinstance(s, pu.Placed) for s in outcome.updated.placements.values()
        )
        return "Updated, with warnings" if warned else "Updated"
    if isinstance(outcome, ur.NotReached):
        return "Not applied: the run stopped first"
    failure = outcome.failure
    if isinstance(failure, du.FamilyChangedSincePreview):
        return "Not applied: the family changed since the preview; check again"
    if isinstance(failure, du.CandidateChangedSincePreview):
        return f"Not applied: the {_role(failure.role)} changed on the share since the preview; check again"
    if isinstance(failure, du.StorageFailed):
        return f"Not applied: a copy failed ({failure.cause})"
    if isinstance(failure, du.DocumentRejected):
        return f"Not applied: the {_role(failure.role)} was rejected ({failure.reason})"
    if isinstance(failure, du.IdentityMismatch):
        return f"Not applied: {failure.field_key} is {failure.found}, expected {failure.expected}"
    if isinstance(failure, du.WriteFailed):
        return f"Not applied: the database write failed ({failure.cause})"
    if isinstance(failure, du.Cancelled):
        return "Cancelled"
    return f"Not applied: {type(failure).__name__}"


def _requirement_lines(diff: Any) -> list[str]:
    before = getattr(diff.before, "states", None)
    after = diff.after.states
    parts = []
    for kind, label in REQUIREMENT_LABELS:
        after_word = STATE_WORDS.get(str(getattr(after.get(_kind(kind)), "value", after.get(_kind(kind)))), "?")
        if before is None:
            before_word = "not scanned"
        else:
            state = before.get(_kind(kind))
            before_word = STATE_WORDS.get(str(getattr(state, "value", state)), "?")
        parts.append(f"{label} {before_word} -> {after_word}")
    lines = ["Requirements  " + "; ".join(parts)]
    if diff.tick_will_need_recheck:
        lines.append("              your X-ray tick will need re-checking")
    return lines


def _kind(value: str) -> Any:
    from cockpit.services.requirements import RequirementKind
    return RequirementKind(value)


def _list(designators: Iterable[str], limit: int = 12) -> str:
    items = sorted(designators)
    text = ", ".join(items[:limit])
    if len(items) > limit:
        text += f", and {len(items) - limit} more"
    return text or "none"


def details_text(record: ur.FamilyRecord) -> str:
    """§7.4's details pane, and §7.5's outcome once the family has one."""
    digest = record.digest
    if digest is None:
        return f"{record.key.part_number}: not checked"
    audits = ", ".join(f"{digest.key.part_number}{s}" for s in digest.suffixes)
    article = "first article" if (digest.article_revision or "FA").upper() == "FA" else f"{digest.article_revision} article"
    lines = [f"{digest.key.part_number} ({article}): audits {audits}"]
    if digest.failure is not None:
        failure = digest.failure
        lines.append(f"Not checked: {_failure_text(failure)}")
        if isinstance(failure, du.ArticleFolderAmbiguous):
            lines.extend(f"  {path.name}" for path in failure.candidates)
        return "\n".join(lines)

    preview = record.preview
    for role in DOCUMENT_ROLES:
        status = digest.statuses.get(role)
        held = digest.stored_files.get(role)
        label = f"{_role(role):<9} "
        update = preview.updates.get(role) if preview is not None else None
        candidate = getattr(update, "candidate", None) or getattr(status, "candidate", None)
        if candidate is not None:
            if held is not None:
                lines.append(f"{label}held  {held[0]} · {_tag(held[0])} · modified {_when(held[1])}")
                label = " " * 10
            lines.append(f"{label}share {candidate.name} · {_tag(candidate.name)} · modified {_when(candidate.modified_at_ns)}")
            if update is not None and update.older_reason:
                lines.append(f"          older on share: {update.older_reason}")
        elif isinstance(status, du.NeedsChoice):
            lines.append(f"{label}pick one of: " + ", ".join(c.name for c in status.candidates))
        elif isinstance(status, du.NotStored):
            lines.append(f"{label}Cockpit holds no {_role(role)} for this audit; re-ingest to add one")
        if preview is not None and role in preview.rejected:
            rejection = preview.rejected[role]
            if isinstance(rejection, du.IdentityMismatch):
                lines.append(f"          refused: {rejection.field_key} is {rejection.found}, expected {rejection.expected}")
            else:
                lines.append(f"          rejected: {rejection.reason}")
        if role == SourceRole.BOM and preview is not None and preview.bom is not None:
            bom = preview.bom
            lines.append(f"          lines added {len(bom.lines_added)}, removed {len(bom.lines_removed)}, "
                         f"changed {len(bom.lines_changed)}; designators added {_list(bom.designators_added)}, "
                         f"removed {_list(bom.designators_removed)}")
            cleaning = _cleaning_text(bom.cleaning)
            lines.append(f"          THT items added {bom.tht_items_added}, removed {bom.tht_items_removed}"
                         + (f"; {cleaning}" if cleaning else ""))
        if role == SourceRole.TRAVELER and preview is not None and preview.traveler is not None:
            traveler = preview.traveler
            changes = "; ".join(f"{c.field_key} {c.before or '(blank)'} -> {c.after or '(blank)'}"
                                for c in traveler.changed_fields)
            if changes:
                lines.append(f"          {changes}")
            if traveler.quantity_on_traveler != traveler.quantity_now:
                lines.append(f"          quantity: traveler {traveler.quantity_on_traveler}, "
                             f"audit keeps {traveler.quantity_now}")
    if preview is not None and preview.requirements is not None:
        lines.extend(_requirement_lines(preview.requirements))
    lines.extend(_placements_lines(record))
    lines.append(KEPT_UNCHANGED)
    if preview is not None and preview.ops_per_board_review:
        lines.append("OPS/board: review it; you entered it against the previous BOM's 2nd OPS parts")
    if record.outcome is not None:
        lines.append("")
        lines.extend(_outcome_lines(record))
    return "\n".join(lines)


def _cleaning_text(cleaning: Any) -> str:
    try:
        from cockpit.ingestion.parsers.audit_bom import describe_routine_cleaning
        return describe_routine_cleaning(cleaning)
    except Exception:
        return ""


def _placements_lines(record: ur.FamilyRecord) -> list[str]:
    digest = record.digest
    preview = record.preview
    placements = digest.placements
    if preview is not None and SourceRole.PLACEMENTS in preview.updates:
        name = preview.updates[SourceRole.PLACEMENTS].candidate.name
        kind = "a changed" if digest.placements_held else "a new"
        return [f"Placements    {kind} BOM Builder file: {name}"]
    if preview is not None and preview.placements_unavailable:
        return [f"Placements    unavailable ({preview.placements_unavailable})"]
    if isinstance(placements, du.ShareReadFailure):
        return [f"Placements    unavailable ({placements.cause})"]
    if isinstance(placements, pu.PlacementsNeedChoice) and SourceRole.PLACEMENTS not in record.picked:
        return ["Placements    pick one of: " + ", ".join(c.name for c in placements.candidates)]
    return []


def _outcome_lines(record: ur.FamilyRecord) -> list[str]:
    outcome = record.outcome
    lines = [f"Outcome: {outcome_text(record)}"]
    if isinstance(outcome, ur.Applied):
        updated = outcome.updated
        if updated.replaced:
            lines.append("Replaced: " + ", ".join(_role(r) for r in updated.replaced))
        for warning in updated.warnings:
            if isinstance(warning, du.DrawingHighlightsIncomplete):
                lines.append(f"Warning: {warning.designators_without_coordinates} new parts won't highlight "
                             f"on the drawing ({warning.cause})")
            elif isinstance(warning, du.PlacementsUnavailable):
                lines.append(f"Warning: placements unavailable: {warning.reason}")
        if updated.quantity_not_applied is not None:
            traveler, audit = updated.quantity_not_applied
            lines.append(f"Quantity not applied: traveler {traveler}, audit keeps {audit}")
        for audit_id, summary in sorted(updated.placements.items()):
            lines.append(f"Audit {audit_id}: {pu.summary_text(summary)}")
    return lines


# ---------- the dialog ----------

class DocumentUpdateDialog(QDialog):
    """Modal, non-blocking: shown with show(), so the run's events keep arriving (§7.1)."""

    apply_requested = pyqtSignal(object)      # tuple[FamilyKey, ...]
    pick_requested = pyqtSignal(object, object)  # FamilyKey, Mapping[SourceRole, Path]
    stop_comparing_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, records: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Check for document updates")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.resize(1100, 640)
        self._records = records
        self._order: list = []
        self._show_all = False
        self._comparing = True
        self._applied = False
        self._finished = False
        self._pending_picks: set = set()
        self._picker = None
        self._closing_by_run = False

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["", "Job", "What changed", "Outcome"])
        self.table.horizontalHeader().setSectionResizeMode(COLUMN_TICK, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COLUMN_JOB, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(COLUMN_CHANGED, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(COLUMN_OUTCOME, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._on_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        left_layout.addWidget(self.table)
        hidden_row = QHBoxLayout()
        self.hidden_label = QLabel("")
        self.hidden_label.setTextFormat(Qt.TextFormat.PlainText)
        hidden_row.addWidget(self.hidden_label, stretch=1)
        self.show_all_btn = QPushButton("Show all")
        self.show_all_btn.setCheckable(True)
        self.show_all_btn.toggled.connect(self._on_show_all)
        hidden_row.addWidget(self.show_all_btn)
        left_layout.addLayout(hidden_row)
        splitter.addWidget(left)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        splitter.addWidget(self.details)
        splitter.setSizes([560, 540])
        layout.addWidget(splitter, stretch=1)

        footer = QHBoxLayout()
        self.progress_label = QLabel("")
        self.progress_label.setTextFormat(Qt.TextFormat.PlainText)
        self.progress_label.setWordWrap(True)
        footer.addWidget(self.progress_label, stretch=1)
        self.stop_comparing_btn = QPushButton("Stop comparing")
        self.stop_comparing_btn.clicked.connect(self._on_stop_comparing)
        footer.addWidget(self.stop_comparing_btn)
        self.pick_btn = QPushButton("Pick…")
        self.pick_btn.clicked.connect(self._on_pick)
        footer.addWidget(self.pick_btn)
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self._on_apply)
        footer.addWidget(self.apply_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self._on_cancel)
        footer.addWidget(self.cancel_btn)
        layout.addLayout(footer)
        self._refresh_buttons()

    # --- rows ---

    def show_rows(self, keys: list) -> None:
        self._order = list(keys)
        self._rebuild()

    def _visible_keys(self) -> list:
        return [k for k in self._order if self._show_all or is_listed(self._records[k])]

    def _rebuild(self) -> None:
        selected = self.selected_key()
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for key in self._visible_keys():
            self._append_row(key)
        self.table.blockSignals(False)
        self.hidden_label.setText(hidden_line(self._records[k] for k in self._order) if not self._show_all else "")
        self.show_all_btn.setVisible(any(not is_listed(self._records[k]) for k in self._order))
        if selected is not None:
            self.select_key(selected)
        elif self.table.rowCount():
            self.table.selectRow(0)
        self._refresh_buttons()

    def _append_row(self, key) -> None:
        record = self._records[key]
        row = self.table.rowCount()
        self.table.insertRow(row)
        tick = QTableWidgetItem()
        tick.setData(Qt.ItemDataRole.UserRole, key)
        self._style_tick(tick, record)
        self.table.setItem(row, COLUMN_TICK, tick)
        for column, text in ((COLUMN_JOB, job_text(record.digest) if record.digest else key.part_number),
                             (COLUMN_CHANGED, what_changed(record)),
                             (COLUMN_OUTCOME, outcome_text(record))):
            item = QTableWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.table.setItem(row, column, item)

    def _tickable(self, record: ur.FamilyRecord) -> bool:
        return (record.preview is not None and record.preview.can_apply and not self._applied
                and record.key not in self._pending_picks)

    def _style_tick(self, item: QTableWidgetItem, record: ur.FamilyRecord) -> None:
        flags = Qt.ItemFlag.ItemIsSelectable
        if self._tickable(record):
            flags |= Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
        else:
            flags |= Qt.ItemFlag.ItemIsEnabled
        item.setFlags(flags)
        if record.preview is not None and record.preview.can_apply:
            item.setCheckState(Qt.CheckState.Checked if record.ticked else Qt.CheckState.Unchecked)

    def _row_of(self, key) -> int:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, COLUMN_TICK)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == key:
                return row
        return -1

    def refresh_row(self, key) -> None:
        row = self._row_of(key)
        record = self._records[key]
        if row < 0:
            if is_listed(record) or self._show_all:
                self._rebuild()
            return
        self.table.blockSignals(True)
        self._style_tick(self.table.item(row, COLUMN_TICK), record)
        self.table.item(row, COLUMN_CHANGED).setText(what_changed(record))
        self.table.item(row, COLUMN_OUTCOME).setText(outcome_text(record))
        self.table.blockSignals(False)
        if self.selected_key() == key:
            self.details.setPlainText(details_text(record))
        self._refresh_buttons()

    def selected_key(self):
        items = self.table.selectedItems()
        return items[0].data(Qt.ItemDataRole.UserRole) if items else None

    def select_key(self, key) -> None:
        row = self._row_of(key)
        if row >= 0:
            self.table.selectRow(row)

    def _on_selection(self) -> None:
        key = self.selected_key()
        self.details.setPlainText(details_text(self._records[key]) if key is not None else "")
        self._refresh_buttons()

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != COLUMN_TICK:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        record = self._records.get(key)
        if record is None or not self._tickable(record):
            return
        record.ticked = item.checkState() == Qt.CheckState.Checked
        self._refresh_buttons()

    def _on_show_all(self, checked: bool) -> None:
        self._show_all = checked
        self.show_all_btn.setText("Show listed only" if checked else "Show all")
        self._rebuild()

    # --- the run's progress ---

    def update_preview(self, key, preview: du.FamilyPreview) -> None:
        record = self._records[key]
        was_pending = key in self._pending_picks
        self._pending_picks.discard(key)
        if record.preview is None or was_pending:
            record.ticked = preview.ticked_by_default
        record.preview = preview
        self.refresh_row(key)

    def comparing_done(self) -> None:
        self._comparing = False
        self._refresh_buttons()

    def set_progress(self, text: str) -> None:
        self.progress_label.setText(text)

    def set_outcome(self, key) -> None:
        self.refresh_row(key)

    def run_finished(self, text: str) -> None:
        self._finished = True
        self._comparing = False
        self.close_picker()
        for key in self._order:
            self.refresh_row(key)
        self.progress_label.setText(text)
        self._refresh_buttons()

    def close_picker(self) -> None:
        if self._picker is not None:
            self._closing_by_run = True
            self._picker.reject()

    # --- buttons ---

    def _ticked_keys(self) -> tuple:
        return tuple(k for k in self._order if self._records[k].ticked and self._tickable(self._records[k]))

    def _refresh_buttons(self) -> None:
        key = self.selected_key()
        record = self._records.get(key) if key is not None else None
        can_pick = (record is not None and not self._applied and not self._finished and record.digest is not None
                    and (any(isinstance(s, du.NeedsChoice) for s in record.digest.statuses.values())
                         or isinstance(record.digest.placements, pu.PlacementsNeedChoice))
                    and key not in self._pending_picks)
        self.pick_btn.setEnabled(bool(can_pick))
        self.stop_comparing_btn.setVisible(self._comparing and not self._finished)
        self.apply_btn.setEnabled(not self._comparing and not self._applied and not self._finished
                                  and not self._pending_picks and bool(self._ticked_keys()))
        self.apply_btn.setVisible(not self._applied and not self._finished)
        self.cancel_btn.setText("Close" if self._finished else "Cancel")
        if self._finished:
            self.cancel_btn.setEnabled(True)

    def _on_stop_comparing(self) -> None:
        self.stop_comparing_btn.setEnabled(False)
        self.stop_comparing_requested.emit()

    def _on_apply(self) -> None:
        keys = self._ticked_keys()
        if not keys:
            return
        self._applied = True
        for key in self._order:
            self.refresh_row(key)
        self._refresh_buttons()
        self.apply_requested.emit(keys)

    def _on_pick(self) -> None:
        key = self.selected_key()
        record = self._records.get(key) if key is not None else None
        if record is None or record.digest is None:
            return
        from cockpit.ingestion.locator import CandidateSet, PendingSelection
        from cockpit.ui.widgets.fetch_dialog import SelectSourcesDialog

        sets = []
        for role in DOCUMENT_ROLES:
            status = record.digest.statuses.get(role)
            if isinstance(status, du.NeedsChoice):
                sets.append(CandidateSet(role, [c.path for c in status.candidates], None))
        placements = record.digest.placements
        if isinstance(placements, pu.PlacementsNeedChoice):
            sets.append(CandidateSet(SourceRole.PLACEMENTS, [c.path for c in placements.candidates], None))
        if not sets:
            return
        pending = PendingSelection(job_number=key.part_number, job_directory=pathlib.Path(),
                                   sets=sets, resolved={}, ignored_count=0)
        self._closing_by_run = False
        self._picker = SelectSourcesDialog(pending, self, optional_roles=frozenset({SourceRole.PLACEMENTS}))
        try:
            accepted = self._picker.exec() == QDialog.DialogCode.Accepted
            choices = dict(self._picker.choices)
        finally:
            self._picker = None
        if not accepted or self._closing_by_run or not choices:
            return
        record.picked = frozenset(record.picked | set(choices))
        self._pending_picks.add(key)
        self.refresh_row(key)
        self.pick_requested.emit(key, choices)

    def _on_cancel(self) -> None:
        if self._finished:
            self.accept()
            return
        self.cancel_btn.setEnabled(False)
        self.cancel_requested.emit()

    def closeEvent(self, event) -> None:
        if self._finished:
            event.accept()
            return
        event.ignore()
        self._on_cancel()

    def reject(self) -> None:
        if self._finished:
            super().reject()
            return
        self._on_cancel()

    def finish_and_close(self) -> None:
        """For the run: close without asking, as at a closeEvent."""
        self._finished = True
        self.close_picker()
        super().reject()
