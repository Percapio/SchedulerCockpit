"""The document-update run, without Qt (Phase 55b §2, §5).

UpdateRunLoop is what the update worker runs: it probes the snapshots the UI thread queues, compares
the families with something to show, takes your picks, and prepares what you confirm. It never
touches the database; the UI thread applies each plan it emits. The summary and the last-run line
are worded here too, so they can be tested without a window.
"""

from __future__ import annotations

import logging
import pathlib
import queue
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Mapping, Optional, Sequence, Union

from cockpit.ingestion.roles import SourceRole
from cockpit.services import document_updates as du
from cockpit.services import placement_updates as pu
from cockpit.services.share_access import JobFolderIndex, guarded

logger = logging.getLogger(__name__)

SNAPSHOT_BATCH = 50
COMMAND_WAIT_SECONDS = 0.1


# ---------- the request ----------

class ConfirmMode(StrEnum):
    PREVIEW = "preview"
    AUTOMATIC = "automatic"


@dataclass(frozen=True)
class RunRequest:
    roles: frozenset[SourceRole]
    confirm: ConfirmMode
    run_id: str


# ---------- commands, from the UI thread ----------

@dataclass(frozen=True)
class Snapshots:
    batch: tuple[du.FamilySnapshot, ...]


@dataclass(frozen=True)
class SnapshotsComplete:
    pass


@dataclass(frozen=True)
class Pick:
    key: du.FamilyKey
    choices: Mapping[SourceRole, pathlib.Path]


@dataclass(frozen=True)
class StopComparing:
    pass


@dataclass(frozen=True)
class Prepare:
    keys: tuple[du.FamilyKey, ...]


@dataclass(frozen=True)
class Finish:
    pass


WorkerCommand = Union[Snapshots, SnapshotsComplete, Pick, StopComparing, Prepare, Finish]


# ---------- events, to the UI thread ----------

@dataclass(frozen=True)
class ProbeDigest:
    """What the table shows of a probed family, without its snapshot (§5.2's memory rule)."""

    key: du.FamilyKey
    members: tuple[int, ...]
    suffixes: tuple[str, ...]
    reference_member: int
    article_revision: str
    statuses: Mapping[SourceRole, Any]
    placements: Any
    failure: Any
    to_compare: bool
    stored_files: Mapping[SourceRole, tuple[str, Optional[int]]] = field(default_factory=dict)
    placements_held: bool = False


@dataclass(frozen=True)
class Probed:
    digest: ProbeDigest


@dataclass(frozen=True)
class ProbingDone:
    pass


@dataclass(frozen=True)
class PreviewReady:
    preview: du.FamilyPreview


@dataclass(frozen=True)
class ComparingDone:
    stopped_early: bool


@dataclass(frozen=True)
class PlanReady:
    plan: du.FamilyUpdatePlan


@dataclass(frozen=True)
class PrepareFailedFor:
    key: du.FamilyKey
    failed: du.PrepareFailed


@dataclass(frozen=True)
class CancelledByYou:
    pass


@dataclass(frozen=True)
class NoAnswer:
    pass


@dataclass(frozen=True)
class WorkerFailed:
    cause: str


StopReason = Union[CancelledByYou, NoAnswer, du.ShareUnreachable, WorkerFailed]


@dataclass(frozen=True)
class RunStopped:
    reason: StopReason


@dataclass(frozen=True)
class RunDone:
    pass


WorkerEvent = Union[Probed, ProbingDone, PreviewReady, ComparingDone, PlanReady, PrepareFailedFor, RunStopped, RunDone]


# ---------- the loop ----------

class _Stopped(Exception):
    pass


def needs_compare(probe: du.FamilyProbe) -> bool:
    """§5.2's filter: a role to update or pick, or placements to show."""
    if any(isinstance(s, (du.Changed, du.OlderOnShare, du.NeedsChoice)) for s in probe.statuses.values()):
        return True
    return isinstance(probe.placements, (pu.PlacementsChanged, pu.PlacementsNeedChoice, du.ShareReadFailure))


def digest_of(snapshot: du.FamilySnapshot, outcome: du.FamilyProbe | Any) -> ProbeDigest:
    members = tuple(m.audit_id for m in snapshot.members)
    suffixes = tuple(m.split_suffix for m in snapshot.members)
    reference = snapshot.stored.get(snapshot.reference_member, {})
    stored_files = {role: (doc.original_filename, doc.modified_at_ns) for role, doc in reference.items()}
    placements_held = any(SourceRole.PLACEMENTS in snapshot.stored.get(a, {}) for a in members)
    if isinstance(outcome, du.FamilyProbe):
        return ProbeDigest(
            key=snapshot.key, members=members, suffixes=suffixes, reference_member=snapshot.reference_member,
            article_revision=snapshot.article_revision, statuses=dict(outcome.statuses),
            placements=outcome.placements, failure=None, to_compare=needs_compare(outcome),
            stored_files=stored_files, placements_held=placements_held,
        )
    return ProbeDigest(
        key=snapshot.key, members=members, suffixes=suffixes, reference_member=snapshot.reference_member,
        article_revision=snapshot.article_revision, statuses={}, placements=None, failure=outcome,
        to_compare=False, stored_files=stored_files, placements_held=placements_held,
    )


class UpdateRunLoop:
    """Runs one check off the UI thread: probe, compare, then prepare what you confirm (§5.2).

    pre:  run on the update worker's thread; commands come from the UI thread through the queue
    post: per §5.2's contract; the database is never touched; every share call goes through
          share_call; snapshots are kept only for families still to be compared, picked or prepared
    """

    def __init__(
        self,
        request: RunRequest,
        commands: "queue.Queue[WorkerCommand]",
        stop: threading.Event,
        emit: Callable[[Any], None],
        source_root: pathlib.Path,
        storage_root: pathlib.Path,
        parsers: Any,
        share_call: Any = None,
        read_hash: du.HashReader = du.read_share_hash,
        copy_file: Callable[[pathlib.Path, pathlib.Path], Any] = shutil.copy2,
        wait_seconds: float = COMMAND_WAIT_SECONDS,
    ) -> None:
        self.request = request
        self.commands = commands
        self.stop = stop
        self.emit = emit
        self.source_root = pathlib.Path(source_root)
        self.storage_root = pathlib.Path(storage_root)
        self.parsers = parsers
        self.guard = guarded(share_call)
        self.read_hash = read_hash
        self.copy_file = copy_file
        self.wait_seconds = wait_seconds
        self.index = JobFolderIndex(self.source_root, self.guard)
        self.previewer = du.requirement_previewer(self.guard, parsers)
        self._probes: dict[du.FamilyKey, du.FamilyProbe] = {}
        self._order: list[du.FamilyKey] = []
        self._choices: dict[du.FamilyKey, dict[SourceRole, pathlib.Path]] = {}
        self._previews: dict[du.FamilyKey, du.FamilyPreview] = {}
        self._compared: set[du.FamilyKey] = set()
        self._stop_comparing = False
        self._comparing_over = False
        self._finished = False
        self._held: list[WorkerCommand] = []

    # --- the queue ---

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise _Stopped()

    def _next_command(self) -> WorkerCommand:
        while True:
            self._check_stop()
            if self._held:
                return self._held.pop(0)
            try:
                return self.commands.get(timeout=self.wait_seconds)
            except queue.Empty:
                continue

    def _pending_commands(self) -> list[WorkerCommand]:
        found: list[WorkerCommand] = []
        while True:
            try:
                found.append(self.commands.get_nowait())
            except queue.Empty:
                return found

    # --- the run ---

    def run(self) -> None:
        try:
            if not self._probe_all():
                return
            self._compare_all()
            if self._finished:
                return
            if self.request.confirm == ConfirmMode.AUTOMATIC:
                keys = tuple(k for k in self._order if k in self._previews and self._previews[k].can_apply)
                self._prepare(keys)
                self.emit(RunDone())
                return
            self._wait_for_you()
        except _Stopped:
            self.emit(RunStopped(CancelledByYou()))
        except Exception as error:
            # By kind only: a parser's or the share's message can carry a path or document text.
            logger.error("The update run stopped: %s", type(error).__name__)
            self.emit(RunStopped(WorkerFailed(type(error).__name__)))

    def _probe_all(self) -> bool:
        while True:
            command = self._next_command()
            if isinstance(command, SnapshotsComplete):
                self.emit(ProbingDone())
                return True
            if isinstance(command, Finish):
                self.emit(RunDone())
                return False
            if not isinstance(command, Snapshots):
                continue
            for snapshot in command.batch:
                self._check_stop()
                outcome = du.probe_family(
                    snapshot, self.source_root, self.read_hash, self.request.roles,
                    share_call=self.guard, job_index=self.index,
                )
                self.emit(Probed(digest_of(snapshot, outcome)))
                if isinstance(outcome, du.ShareUnreachable):
                    self.emit(RunStopped(outcome))
                    return False
                if isinstance(outcome, du.FamilyProbe) and needs_compare(outcome):
                    self._probes[snapshot.key] = outcome
                    self._order.append(snapshot.key)

    def _preview(self, key: du.FamilyKey) -> du.FamilyPreview:
        preview = du.build_preview(
            self._probes[key], self._choices.get(key, {}), self.parsers, self.source_root,
            requirements=self.previewer, read_hash=self.read_hash, share_call=self.guard,
        )
        self._previews[key] = preview
        self._compared.add(key)
        return preview

    def _serve(self, command: WorkerCommand) -> None:
        """A command that can arrive while comparing or waiting."""
        if isinstance(command, Pick):
            if command.key not in self._probes:
                return
            self._choices.setdefault(command.key, {}).update(command.choices)
            if command.key in self._compared or self._comparing_over:
                self._check_stop()
                self.emit(PreviewReady(self._preview(command.key)))
        elif isinstance(command, StopComparing):
            self._stop_comparing = True
        elif isinstance(command, Finish):
            self._finished = True
        elif isinstance(command, Prepare):
            # Too early while comparing: kept for the waiting stage, never dropped.
            self._held.append(command)

    def _compare_all(self) -> None:
        automatic = self.request.confirm == ConfirmMode.AUTOMATIC
        for key in self._order:
            if not automatic:
                for command in self._pending_commands():
                    self._serve(command)
                if self._finished:
                    self.emit(RunDone())
                    return
                if self._stop_comparing:
                    break
            self._check_stop()
            preview = self._preview(key)
            if not automatic:
                self.emit(PreviewReady(preview))
        if not automatic:
            # A pick queued for a family that Stop comparing skipped still gets its re-diff.
            for key in self._order:
                if key not in self._compared and key in self._choices:
                    self._check_stop()
                    self.emit(PreviewReady(self._preview(key)))
            self._comparing_over = True
            self.emit(ComparingDone(stopped_early=self._stop_comparing))

    def _wait_for_you(self) -> None:
        while True:
            command = self._next_command()
            if isinstance(command, Prepare):
                self._prepare(command.keys)
                self.emit(RunDone())
                return
            self._serve(command)
            if self._finished:
                self.emit(RunDone())
                return

    def _prepare(self, keys: Sequence[du.FamilyKey]) -> None:
        for key in keys:
            self._check_stop()
            preview = self._previews.get(key)
            if preview is None or not preview.can_apply:
                continue
            outcome = du.prepare_family_update(
                preview, self.storage_root, self.source_root, self.parsers, self.stop, self.request.run_id,
                copy_file=self.copy_file, share_call=self.guard,
            )
            if isinstance(outcome, du.PrepareFailed):
                self.emit(PrepareFailedFor(key, outcome))
                if isinstance(outcome.failure, du.Cancelled):
                    raise _Stopped()
            else:
                self.emit(PlanReady(outcome))
            self._probes.pop(key, None)


# ---------- outcomes and the summary (§2) ----------

@dataclass(frozen=True)
class Applied:
    updated: du.FamilyUpdated


@dataclass(frozen=True)
class NotApplied:
    failure: Any


@dataclass(frozen=True)
class NotReached:
    pass


FamilyOutcome = Union[Applied, NotApplied, NotReached]


class StopCode(StrEnum):
    CANCELLED = "cancelled"
    NO_ANSWER = "no_answer"
    SHARE_UNREACHABLE = "share_unreachable"
    WORKER_FAILED = "worker_failed"


def stop_code(reason: StopReason | None) -> StopCode | None:
    if reason is None:
        return None
    if isinstance(reason, CancelledByYou):
        return StopCode.CANCELLED
    if isinstance(reason, NoAnswer):
        return StopCode.NO_ANSWER
    if isinstance(reason, du.ShareUnreachable):
        return StopCode.SHARE_UNREACHABLE
    return StopCode.WORKER_FAILED


STOP_WORDS: dict[StopCode, str] = {
    StopCode.CANCELLED: "cancelled",
    StopCode.NO_ANSWER: "no answer from the share",
    StopCode.SHARE_UNREACHABLE: "the share couldn't be reached",
    StopCode.WORKER_FAILED: "an internal error",
}


@dataclass
class FamilyRecord:
    """What the run knows of one family, for its row and the summary."""

    key: du.FamilyKey
    digest: Optional[ProbeDigest] = None
    preview: Optional[du.FamilyPreview] = None
    ticked: bool = False
    outcome: Optional[FamilyOutcome] = None
    picked: frozenset[SourceRole] = frozenset()


@dataclass(frozen=True)
class UpdateRunSummary:
    families_in_run: int = 0
    families_checked: int = 0
    updated: int = 0
    failed: int = 0
    not_reached: int = 0
    rejected: int = 0
    needs_choice: int = 0
    older_on_share: int = 0
    skipped: int = 0
    not_checked: int = 0
    missing: int = 0
    unchanged: int = 0
    placements_placed: int = 0
    placements_not_used: int = 0
    stopped_early: Optional[StopCode] = None


def bucket_of(record: FamilyRecord) -> str:
    """The one bucket a family counts in: the first of §2's rules that applies."""
    outcome = record.outcome
    if isinstance(outcome, Applied):
        return "updated"
    if record.digest is None:
        return "not_checked"
    digest = record.digest
    if digest.failure is not None and not isinstance(digest.failure, du.Missing):
        return "failed"
    if isinstance(outcome, NotApplied) and not isinstance(outcome.failure, du.Cancelled):
        return "failed"
    if isinstance(outcome, NotReached) or (isinstance(outcome, NotApplied) and isinstance(outcome.failure, du.Cancelled)):
        return "not_reached"
    preview = record.preview
    if preview is not None and preview.rejected:
        return "rejected"
    if preview is not None and (preview.awaiting_choice or _placements_unpicked(digest, record)):
        return "needs_choice"
    if preview is not None and any(u.older_reason for u in preview.updates.values()) and not record.ticked:
        return "older_on_share"
    if preview is not None and preview.updates and not record.ticked:
        return "skipped"
    if digest.to_compare and preview is None:
        return "not_checked"
    if digest.failure is not None or any(isinstance(s, du.Missing) for s in digest.statuses.values()):
        return "missing"
    return "unchanged"


def _placements_unpicked(digest: ProbeDigest, record: FamilyRecord) -> bool:
    return isinstance(digest.placements, pu.PlacementsNeedChoice) and SourceRole.PLACEMENTS not in record.picked


def summarize(
    keys_in_run: int,
    dropped: int,
    records: Sequence[FamilyRecord],
    stop: StopReason | None,
) -> UpdateRunSummary:
    """§2's summary: every family in exactly one bucket; placements counts exclusive, over applied families."""
    counts: dict[str, int] = {}
    placed = not_used = 0
    by_key = {r.key: r for r in records}
    in_run = max(keys_in_run - dropped, 0)
    for record in by_key.values():
        bucket = bucket_of(record)
        counts[bucket] = counts.get(bucket, 0) + 1
        if isinstance(record.outcome, Applied) and record.outcome.updated.placements:
            summaries = record.outcome.updated.placements.values()
            if all(isinstance(s, pu.Placed) for s in summaries):
                placed += 1
            else:
                not_used += 1
    unseen = max(in_run - len(by_key), 0)
    return UpdateRunSummary(
        families_in_run=in_run,
        families_checked=sum(1 for r in by_key.values() if r.digest is not None),
        updated=counts.get("updated", 0),
        failed=counts.get("failed", 0),
        not_reached=counts.get("not_reached", 0),
        rejected=counts.get("rejected", 0),
        needs_choice=counts.get("needs_choice", 0),
        older_on_share=counts.get("older_on_share", 0),
        skipped=counts.get("skipped", 0),
        not_checked=counts.get("not_checked", 0) + unseen,
        missing=counts.get("missing", 0),
        unchanged=counts.get("unchanged", 0),
        placements_placed=placed,
        placements_not_used=not_used,
        stopped_early=stop_code(stop),
    )


# ---------- the last-run line (§8.1) ----------

@dataclass(frozen=True)
class StoredRun:
    finished_at: datetime
    summary: UpdateRunSummary


LINE_COUNTS: tuple[tuple[str, str], ...] = (
    ("updated", "updated"),
    ("failed", "failed"),
    ("not_reached", "stopped before applying"),
    ("rejected", "rejected"),
    ("needs_choice", "needs a choice"),
    ("older_on_share", "older on share"),
    ("skipped", "left unticked"),
    ("not_checked", "not checked"),
)


def counts_text(summary: UpdateRunSummary) -> str:
    parts = [f"{getattr(summary, name):,} {words}" for name, words in LINE_COUNTS if getattr(summary, name)]
    return ", ".join(parts) if parts else "nothing to update"


def last_run_line(stored: Optional[StoredRun]) -> str:
    """Words the last run for Settings (§8.1)."""
    if stored is None:
        return "Never checked"
    when = stored.finished_at.astimezone()
    text = f"Last checked {when.day} {when:%b %H:%M}: {counts_text(stored.summary)}"
    if stored.summary.stopped_early is not None:
        text += f"; stopped: {STOP_WORDS[stored.summary.stopped_early]}"
    return text


def stored_run_to_json(stored: StoredRun) -> dict[str, Any]:
    summary = stored.summary
    payload = {name: getattr(summary, name) for name in UpdateRunSummary.__dataclass_fields__}
    payload["stopped_early"] = summary.stopped_early.value if summary.stopped_early else None
    return {"finished_at": stored.finished_at.isoformat(), "summary": payload}


def stored_run_from_json(payload: Any) -> Optional[StoredRun]:
    try:
        summary_fields = dict(payload["summary"])
        stop = summary_fields.pop("stopped_early", None)
        known = {k: int(v) for k, v in summary_fields.items() if k in UpdateRunSummary.__dataclass_fields__}
        summary = UpdateRunSummary(**known, stopped_early=StopCode(stop) if stop else None)
        return StoredRun(datetime.fromisoformat(payload["finished_at"]), summary)
    except (KeyError, TypeError, ValueError):
        return None


def summary_line(summary: UpdateRunSummary) -> str:
    """The dialog's footer once the run ends (§7.5)."""
    text = f"Checked {summary.families_checked:,} of {summary.families_in_run:,} families: {counts_text(summary)}"
    if summary.placements_placed or summary.placements_not_used:
        text += f"; placements placed for {summary.placements_placed:,}"
        if summary.placements_not_used:
            text += f", not used for {summary.placements_not_used:,}"
    if summary.stopped_early is not None:
        text += f"; stopped: {STOP_WORDS[summary.stopped_early]}"
    return text


# ---------- the check after ingest's toast (§9) ----------

def placements_check_message(record: Optional[FamilyRecord], stop: StopReason | None) -> Optional[str]:
    """The one toast a check after ingest shows, or None when there is nothing to say."""
    if isinstance(stop, NoAnswer):
        return "Placements not read: the share didn't answer. Check for updates… will try again"
    if isinstance(stop, WorkerFailed):
        return f"Placements not read: the check stopped ({stop.cause})"
    if isinstance(stop, du.ShareUnreachable):
        return f"Placements not read: {stop.cause}"
    if record is None or record.digest is None:
        return None
    digest = record.digest
    failure = digest.failure
    if failure is not None:
        if isinstance(failure, du.Missing):
            return None
        if isinstance(failure, du.EscapesRoot):
            return "Placements not read: the job folder is outside the Source Root"
        if isinstance(failure, du.ArticleFolderAmbiguous):
            return "Placements not read: two folders claim this article"
        return f"Placements not read: {getattr(failure, 'cause', type(failure).__name__)}"
    placements = digest.placements
    if isinstance(placements, pu.PlacementsNeedChoice):
        return f"{len(placements.candidates)} .bmb files in the job folder; choose one in Check for updates…"
    if isinstance(placements, du.ShareReadFailure):
        if placements.cause == "ESCAPES_ROOT":
            return "Placements not read: the job folder is outside the Source Root"
        return f"Placements not read: {placements.cause}"
    outcome = record.outcome
    if isinstance(outcome, NotApplied):
        failure = outcome.failure
        if isinstance(failure, du.FamilyChangedSincePreview):
            return "The audit changed while placements were read. Check for updates… will bring them"
        if isinstance(failure, du.StorageFailed):
            return f"Placements not stored: {failure.cause}"
        if isinstance(failure, du.WriteFailed):
            return f"Placements not saved: {failure.cause}"
        return f"Placements not read: {type(failure).__name__}"
    if isinstance(outcome, Applied):
        summaries = outcome.updated.placements
        if not summaries:
            return None
        reference = summaries.get(digest.reference_member) or next(iter(summaries.values()))
        text = _placements_toast_text(reference)
        count = len(summaries)
        if count > 1:
            same = all(s == reference for s in summaries.values())
            text += f" for {count} audits" if same else f" for 1 of {count} audits; see Placements report…"
        return text
    return None


def _placements_toast_text(summary: Any) -> str:
    if isinstance(summary, pu.Placed):
        return pu.summary_text(summary)
    if isinstance(summary, du.WriteFailed):
        return f"Placements not saved: {summary.cause}"
    if isinstance(summary, (pu.NotUsed, pu.Removed)):
        from cockpit.placements.wording import reason_text
        return f"Placements not used: {reason_text(summary.reason)}"
    if isinstance(summary, pu.Kept):
        return f"Placements not used: {summary.reason}"
    return pu.summary_text(summary)
