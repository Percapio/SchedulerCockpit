"""Phase 52: export and check Mirtec MV-3 AOI XY files for one audit.

Every share access runs through start_bounded_probe, so a slow or dead share
costs a status line, never a frozen window, and closing the dialog mid-read is
safe.
"""

import dataclasses
import pathlib
from functools import partial
from typing import Optional, Sequence

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from cockpit.services.aoi.export import (
    AoiExportPlan, AoiExportRequest, AoiWriteReport, prepare_aoi_export, write_aoi_files,
)
from cockpit.services.aoi.errors import AoiDestinationExists
from cockpit.services.aoi.mirtec import compare_aoi, output_file_name
from cockpit.services.aoi.types import (
    AoiComparison, AoiSideDocument, BoardSide, FiducialStatus, FrameStatus, ProgramArticlePending,
    XyNotFound, XyPending,
)
from cockpit.ui.error_messages import render
from cockpit.ui.widgets.qt_lifecycle import FETCH_WATCHDOG_TIMEOUT_MS, start_bounded_probe

LAST_EXPORT_FOLDER_KEY = "aoi/last_export_folder"
_BASE_ARTICLE_LABEL = "(base program, no article)"
_MAX_LISTED = 40


class AoiDialogPrompts:
    """The dialog's modal questions, replaceable in tests."""

    def choose_item(self, parent: QWidget, title: str, label: str, items: list[str]) -> Optional[str]:
        item, ok = QInputDialog.getItem(parent, title, label, items, 0, False)
        return item if ok else None

    def choose_directory(self, parent: QWidget, title: str, start: str) -> str:
        return QFileDialog.getExistingDirectory(parent, title, start)

    def choose_file(self, parent: QWidget, title: str, start: str, file_filter: str) -> str:
        return QFileDialog.getOpenFileName(parent, title, start, file_filter)[0]

    def choose_files(self, parent: QWidget, title: str, start: str, file_filter: str) -> list[str]:
        return QFileDialog.getOpenFileNames(parent, title, start, file_filter)[0]

    def confirm(self, parent: QWidget, title: str, text: str) -> bool:
        answer = QMessageBox.question(parent, title, text,
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return answer == QMessageBox.StandardButton.Yes


def side_from_file_name(name: str) -> Optional[BoardSide]:
    upper = name.upper()
    if upper.startswith("T-"):
        return BoardSide.TOP
    if upper.startswith("B-"):
        return BoardSide.BOTTOM
    return None


def _side_title(side: BoardSide) -> str:
    return "Top" if side is BoardSide.TOP else "Bottom"


def describe_side(document: AoiSideDocument, first_fiducial: int, has_program: bool) -> list[str]:
    lines = [f"{_side_title(document.side)}: {len(document.rows)} parts, {document.fiducial_count} fiducials"]
    if document.frame_status is FrameStatus.PROGRAM_ORIGIN and document.frame is not None:
        mirror = ", mirrored" if document.frame.mirrored_x else ""
        lines.append(f"  Origin: SMT program origin ({document.frame.matched} placements matched{mirror}, "
                     f"max residual {document.frame.max_residual_mil:.2f} mil)")
    else:
        lines.append("  WARNING Origin: the XY file's own origin - it will not match the SMT program")
    if document.fiducial_status is FiducialStatus.PROGRAM_MARKS:
        last = first_fiducial + document.fiducial_count - 1
        lines.append(f"  Fiducials: from the SMT program (FID{first_fiducial}-FID{last})")
    elif document.fiducial_status is FiducialStatus.CAD_ROWS:
        lines.append("  WARNING Fiducials: from the XY file, not the SMT program")
    else:
        lines.append("  WARNING Fiducials: none found - add them in the AOI software")
    if not has_program:
        lines.append("  Note: no SMT program for this side; left out of the export unless ticked")
    for flag in document.flags:
        lines.append(f"  - {flag.message}")
        if flag.designators:
            shown = ", ".join(flag.designators[:_MAX_LISTED])
            more = f" (+{len(flag.designators) - _MAX_LISTED} more)" if len(flag.designators) > _MAX_LISTED else ""
            lines.append(f"      {shown}{more}")
    return lines


def describe_plan(plan: AoiExportPlan, bottom_start: int) -> str:
    lines = [f"XY file: {plan.xy_path}"]
    if plan.programs:
        lines.append("Programs: " + ", ".join(p.path.name for p in plan.programs))
    else:
        lines.append("Programs: none")
    lines += [f"Note: {notice}" for notice in plan.notices]
    for document in plan.documents():
        first = 1 if document.side is BoardSide.TOP else bottom_start
        lines.append("")
        has_program = plan.has_program(document.side) or not plan.programs
        lines += describe_side(document, first, has_program)
    if not plan.documents():
        lines.append("")
        lines.append("The XY file holds no placements.")
    return "\n".join(lines)


def describe_comparison(comparison: AoiComparison) -> list[str]:
    title = f"{_side_title(comparison.side)} vs {comparison.existing.name}"
    if comparison.is_identical:
        return [f"{title}: identical"]
    lines = [f"{title}: differences found"]
    if comparison.missing_in_existing:
        lines.append(f"  Missing from the existing file: {', '.join(comparison.missing_in_existing[:_MAX_LISTED])}")
    if comparison.extra_in_existing:
        lines.append(f"  Only in the existing file: {', '.join(comparison.extra_in_existing[:_MAX_LISTED])}")
    for diff in comparison.differences[:_MAX_LISTED]:
        lines.append(f"  {diff.ref_des} {diff.kind}: Cockpit {diff.expected} / existing {diff.existing}")
    if len(comparison.differences) > _MAX_LISTED:
        lines.append(f"  (+{len(comparison.differences) - _MAX_LISTED} more differences)")
    return lines


def _compare_selected(plan: AoiExportPlan, bottom_start: int,
                      files: Sequence[tuple[BoardSide, pathlib.Path]]) -> list[AoiComparison]:
    results = []
    for side, path in files:
        document = plan.document(side)
        first = 1 if side is BoardSide.TOP else bottom_start
        results.append(compare_aoi(document, first, path))
    return results


class AoiXyDialog(QDialog):
    def __init__(
        self,
        job_number: str,
        stored_bom: pathlib.Path,
        source_root: pathlib.Path,
        program_root: Optional[pathlib.Path],
        forbidden_roots: Sequence[pathlib.Path] = (),
        settings: QSettings | None = None,
        parent: QWidget | None = None,
        prompts: AoiDialogPrompts | None = None,
        watchdog_ms: int = FETCH_WATCHDOG_TIMEOUT_MS,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"AOI XY - {job_number}")
        self.setModal(True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.resize(720, 520)

        self._request = AoiExportRequest(job_number, source_root, stored_bom, program_root)
        self._forbidden_roots = tuple(forbidden_roots)
        self._settings = settings
        self._prompts = prompts or AoiDialogPrompts()
        self._watchdog_ms = watchdog_ms
        self._admission = None
        self.plan: AoiExportPlan | None = None
        self.last_write: AoiWriteReport | None = None
        self.last_comparisons: list[AoiComparison] = []

        layout = QVBoxLayout(self)
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.summary = QPlainTextEdit()
        self.summary.setReadOnly(True)
        layout.addWidget(self.summary, 1)

        sides_row = QHBoxLayout()
        self.top_check = QCheckBox("Top")
        self.bottom_check = QCheckBox("Bottom")
        sides_row.addWidget(QLabel("Export:"))
        sides_row.addWidget(self.top_check)
        sides_row.addWidget(self.bottom_check)
        sides_row.addStretch()
        sides_row.addWidget(QLabel("Bottom fiducials start at FID"))
        self.bottom_start_spin = QSpinBox()
        self.bottom_start_spin.setRange(1, 999)
        self.bottom_start_spin.valueChanged.connect(self._refresh_summary)
        sides_row.addWidget(self.bottom_start_spin)
        layout.addLayout(sides_row)

        buttons = QHBoxLayout()
        self.choose_xy_btn = QPushButton("Choose XY file...")
        self.choose_xy_btn.clicked.connect(self._on_choose_xy_clicked)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.prepare)
        self.export_btn = QPushButton("Export...")
        self.export_btn.clicked.connect(self._on_export_clicked)
        self.check_btn = QPushButton("Check existing...")
        self.check_btn.clicked.connect(self._on_check_clicked)
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        for button in (self.choose_xy_btn, self.refresh_btn, self.export_btn, self.check_btn):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(self.close_btn)
        layout.addLayout(buttons)

        self.finished.connect(self._abandon_in_flight)
        self._apply_plan(None)
        self.prepare()

    # --- state -------------------------------------------------------------

    def is_busy(self) -> bool:
        return self._admission is not None and self._admission.is_in_flight()

    def _set_busy(self, message: str) -> None:
        self.status_label.setText(message)
        for button in (self.choose_xy_btn, self.refresh_btn, self.export_btn, self.check_btn):
            button.setEnabled(False)

    def _set_idle(self, message: str) -> None:
        self.status_label.setText(message)
        self.choose_xy_btn.setEnabled(True)
        self.refresh_btn.setEnabled(True)
        has_plan = self.plan is not None and bool(self.plan.documents())
        self.export_btn.setEnabled(has_plan)
        self.check_btn.setEnabled(has_plan)

    def _abandon_in_flight(self, *_args) -> None:
        if self._admission is not None:
            self._admission.abandon()

    def _run(self, work, on_outcome, busy_message: str) -> None:
        self._set_busy(busy_message)
        self._admission = start_bounded_probe(
            owner=self,
            work=work,
            watchdog_ms=self._watchdog_ms,
            on_outcome=on_outcome,
            on_timeout=self._on_timeout,
            on_error=self._on_error,
        )

    def _on_timeout(self) -> None:
        self._set_idle(f"No answer from the share within {self._watchdog_ms // 1000} seconds. "
                       f"Use Refresh to try again.")

    def _on_error(self, exc: BaseException) -> None:
        payload = render(exc) if isinstance(exc, Exception) else None
        if payload is None:
            self._set_idle("The export stopped; see the log.")
            return
        lines = [payload.summary] + [f"  {key}: {value}" for key, value in payload.detail]
        self.summary.setPlainText("\n".join(lines))
        self._set_idle(payload.title)

    # --- preparation ---------------------------------------------------------

    def prepare(self) -> None:
        self._run(partial(prepare_aoi_export, self._request), self._on_prepared,
                  "Reading the XY file, the Audit BOM and the SMT programs...")

    def _on_prepared(self, outcome) -> None:
        if isinstance(outcome, XyPending):
            names = [p.name for p in outcome.candidates]
            choice = self._prompts.choose_item(self, "Several XY files", "Which XY export is for this job?", names)
            if choice is None:
                self._set_idle("No XY file chosen. Use Choose XY file to pick one.")
                return
            chosen = outcome.candidates[names.index(choice)]
            self._request = dataclasses.replace(self._request, xy_path=chosen)
            self.prepare()
            return
        if isinstance(outcome, XyNotFound):
            self._apply_plan(None)
            self._set_idle(f"No XY export (RefDes,PartNum,XLocation,...) in {outcome.searched}. "
                           f"Use Choose XY file to pick one.")
            return
        if isinstance(outcome, ProgramArticlePending):
            articles = outcome.articles()
            labels = [a if a is not None else _BASE_ARTICLE_LABEL for a in articles]
            choice = self._prompts.choose_item(self, "Several article programs",
                                               "Which build's SMT programs belong to this audit?", labels)
            if choice is None:
                self._set_idle("No program set chosen. Use Refresh to choose again.")
                return
            self._request = dataclasses.replace(
                self._request, article_chosen=True, article=articles[labels.index(choice)])
            self.prepare()
            return
        self._apply_plan(outcome)
        self._set_idle("Ready." if outcome.documents() else "The XY file holds no placements.")

    def _apply_plan(self, plan: AoiExportPlan | None) -> None:
        self.plan = plan
        defaults = plan.default_sides() if plan is not None else ()
        for side, box in ((BoardSide.TOP, self.top_check), (BoardSide.BOTTOM, self.bottom_check)):
            document = plan.document(side) if plan is not None else None
            box.setEnabled(document is not None)
            box.setChecked(side in defaults)
            if document is not None:
                box.setText(f"{_side_title(side)} ({output_file_name(side, plan.job_number)})")
            else:
                box.setText(_side_title(side))
        self.bottom_start_spin.blockSignals(True)
        self.bottom_start_spin.setValue(plan.default_bottom_start() if plan is not None else 1)
        self.bottom_start_spin.blockSignals(False)
        self.bottom_start_spin.setEnabled(plan is not None and plan.bottom is not None)
        self._refresh_summary()

    def _refresh_summary(self, *_args) -> None:
        if self.plan is not None:
            self.summary.setPlainText(describe_plan(self.plan, self.bottom_start_spin.value()))

    def selected_sides(self) -> tuple[BoardSide, ...]:
        chosen = []
        if self.top_check.isEnabled() and self.top_check.isChecked():
            chosen.append(BoardSide.TOP)
        if self.bottom_check.isEnabled() and self.bottom_check.isChecked():
            chosen.append(BoardSide.BOTTOM)
        return tuple(chosen)

    def _start_folder(self) -> str:
        if self._settings is None:
            return ""
        return str(self._settings.value(LAST_EXPORT_FOLDER_KEY, "") or "")

    def _on_choose_xy_clicked(self) -> None:
        path = self._prompts.choose_file(self, "Choose the XY export", str(self._request.source_root),
                                         "XY exports (*.csv *.txt);;All files (*)")
        if not path:
            return
        self._request = dataclasses.replace(self._request, xy_path=pathlib.Path(path))
        self.prepare()

    # --- export --------------------------------------------------------------

    def _on_export_clicked(self) -> None:
        if self.plan is None:
            return
        sides = self.selected_sides()
        if not sides:
            self.status_label.setText("Tick at least one side to export.")
            return
        folder = self._prompts.choose_directory(self, "Export AOI XY files to", self._start_folder())
        if not folder:
            return
        self._write(pathlib.Path(folder), sides, overwrite=False)

    def _write(self, destination: pathlib.Path, sides: tuple[BoardSide, ...], overwrite: bool) -> None:
        work = partial(write_aoi_files, self.plan, destination, self.bottom_start_spin.value(),
                       overwrite, self._forbidden_roots, sides)
        self._set_busy("Writing AOI files...")
        self._admission = start_bounded_probe(
            owner=self,
            work=work,
            watchdog_ms=self._watchdog_ms,
            on_outcome=lambda report, d=destination: self._on_written(report, d),
            on_timeout=self._on_timeout,
            on_error=lambda exc, d=destination, s=sides: self._on_write_error(exc, d, s),
        )

    def _on_write_error(self, exc: BaseException, destination: pathlib.Path,
                        sides: tuple[BoardSide, ...]) -> None:
        if isinstance(exc, AoiDestinationExists):
            names = "\n".join(p.name for p in exc.existing)
            if self._prompts.confirm(self, "Replace AOI files?", f"These files already exist:\n{names}\n\nReplace them?"):
                self._write(destination, sides, overwrite=True)
            else:
                self._set_idle("Nothing was written.")
            return
        self._on_error(exc)

    def _on_written(self, report: AoiWriteReport, destination: pathlib.Path) -> None:
        self.last_write = report
        if self._settings is not None:
            self._settings.setValue(LAST_EXPORT_FOLDER_KEY, str(destination))
        names = ", ".join(p.name for p in report.written)
        replaced = f" (replaced {len(report.replaced)})" if report.replaced else ""
        self._set_idle(f"Wrote {names} to {destination}{replaced}.")

    # --- check ---------------------------------------------------------------

    def _on_check_clicked(self) -> None:
        if self.plan is None:
            return
        paths = self._prompts.choose_files(self, "Choose existing AOI XY file(s)", self._start_folder(),
                                           "AOI XY files (*-XY.txt);;Text files (*.txt);;All files (*)")
        if not paths:
            return
        selected: list[tuple[BoardSide, pathlib.Path]] = []
        skipped: list[str] = []
        for raw in paths:
            path = pathlib.Path(raw)
            side = side_from_file_name(path.name)
            if side is None or self.plan.document(side) is None:
                skipped.append(path.name)
            else:
                selected.append((side, path))
        if not selected:
            self.status_label.setText("Pick files named T-<job>-XY.txt or B-<job>-XY.txt for a side this job has.")
            return
        work = partial(_compare_selected, self.plan, self.bottom_start_spin.value(), tuple(selected))
        self._run(work, lambda results, s=tuple(skipped): self._on_compared(results, s),
                  "Comparing with the existing AOI file(s)...")

    def _on_compared(self, results: list[AoiComparison], skipped: tuple[str, ...]) -> None:
        self.last_comparisons = results
        lines: list[str] = []
        for comparison in results:
            lines += describe_comparison(comparison)
        if skipped:
            lines.append(f"Skipped (no matching side): {', '.join(skipped)}")
        self.summary.setPlainText("\n".join(lines))
        identical = all(c.is_identical for c in results)
        self._set_idle("Identical to the existing file(s)." if identical else "Differences found; see the list.")
