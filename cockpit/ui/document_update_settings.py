"""The last run's line, kept in QSettings (Phase 55b §8.1).

Stored as JSON under LAST_RUN_KEY: the finish time, the counts and the stop code. A code carries no
cause text, so no path or name can reach the stored line.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from PyQt6.QtCore import QSettings

from cockpit.services.update_run import StoredRun, UpdateRunSummary, stored_run_from_json, stored_run_to_json

logger = logging.getLogger(__name__)

LAST_RUN_KEY = "document_updates/last_run"


def store_last_run(settings: Optional[QSettings], summary: UpdateRunSummary) -> None:
    if settings is None:
        return
    stored = StoredRun(datetime.now(timezone.utc), summary)
    settings.setValue(LAST_RUN_KEY, json.dumps(stored_run_to_json(stored)))
    settings.sync()


def load_last_run(settings: Optional[QSettings]) -> Optional[StoredRun]:
    if settings is None:
        return None
    raw = settings.value(LAST_RUN_KEY)
    if not raw:
        return None
    try:
        return stored_run_from_json(json.loads(str(raw)))
    except (TypeError, ValueError):
        logger.warning("The stored last run couldn't be read; it is ignored")
        return None
