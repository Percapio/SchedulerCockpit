"""File hashing module."""

import hashlib
import pathlib

from .errors import HashingError


def sha256_hex(path: pathlib.Path) -> str:
    """Compute a deterministic SHA-256 digest of file bytes."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except IOError as e:
        raise HashingError(path, e)
        
    return h.hexdigest()
