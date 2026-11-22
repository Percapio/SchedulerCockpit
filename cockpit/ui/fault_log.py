"""Keeps faulthandler.log from growing for ever (Patch 14 §10.4).

app.main enables faulthandler before AppConfig exists, so the cap is a constant rather than a setting.
"""

from __future__ import annotations

import os
import pathlib
from typing import Optional

FAULTHANDLER_MAX_BYTES = 1_048_576


def rotate_faulthandler_log(path: pathlib.Path, max_bytes: int = FAULTHANDLER_MAX_BYTES) -> Optional[OSError]:
    """Keeps one backup of a faulthandler log that has grown past max_bytes.

    pre:  called before faulthandler opens path
    post: when path is larger than max_bytes, it has replaced path.1 and path no longer exists, so the
          next open starts a new file; when it isn't, or doesn't exist, nothing changed. A rename that
          fails (either file locked) changes nothing and returns the error, so the caller can log it
          once logging is configured
    raises: nothing
    """
    try:
        if not path.exists() or path.stat().st_size <= max_bytes:
            return None
        os.replace(path, path.with_name(path.name + ".1"))
        return None
    except OSError as error:
        return error
