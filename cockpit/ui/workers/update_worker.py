"""The update worker: UpdateRunLoop on its own QThread (Phase 55b §5.2).

It holds no connection. Commands arrive through a thread-safe queue, because the loop's run doesn't
return until the run ends, so Qt can't deliver queued slots to it. Every share call goes through a
depth-counting guard whose beacon drives the UI thread's watchdog.
"""

from __future__ import annotations

import pathlib
import queue
import threading
from typing import Any

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

from cockpit.services.update_run import RunRequest, UpdateRunLoop
from cockpit.ui.workers.share_watchdog import ShareCallBeacon, share_call_guard


class UpdateWorker(QObject):
    # Not "event": that name would shadow QObject.event(), which Qt calls for every event it delivers.
    run_event = pyqtSignal(object)  # an update_run WorkerEvent

    def __init__(
        self,
        request: RunRequest,
        source_root: pathlib.Path,
        storage_root: pathlib.Path,
        parsers: Any,
    ) -> None:
        super().__init__()
        self.request = request
        self.source_root = source_root
        self.storage_root = storage_root
        self.parsers = parsers
        self.commands: "queue.Queue[Any]" = queue.Queue()
        self.stop = threading.Event()
        self.beacon = ShareCallBeacon()

    @pyqtSlot()
    def run(self) -> None:
        UpdateRunLoop(
            self.request, self.commands, self.stop, self.run_event.emit, self.source_root, self.storage_root,
            self.parsers, share_call=share_call_guard(self.beacon),
        ).run()
