"""Phase 2 error hierarchy."""

import pathlib
from typing import Any, Literal

from cockpit.persistence.errors import PersistenceError


class IngestionError(Exception):
    """Base class for cockpit.ingestion exceptions."""


# ---------- gatekeeper / categorization ----------

class GatekeeperViolation(IngestionError):
    def __init__(self, reason: str, detail: dict[str, Any]):
        super().__init__(f"Gatekeeper violation: {reason} - {detail}")
        self.reason = reason
        self.detail = detail


class CategorizationError(IngestionError):
    def __init__(self, path: pathlib.Path, reason: str):
        super().__init__(f"Could not categorize file {path.name}: {reason}")
        self.path = path
        self.reason = reason


# ---------- hashing / storage ----------

class HashingError(IngestionError):
    def __init__(self, path: pathlib.Path, cause: Exception):
        super().__init__(f"Failed to hash {path.name}: {cause}")
        self.path = path
        self.cause = cause


class FileStorageError(IngestionError):
    """Raised by IngestionService when the pre-transaction copy step fails."""
    def __init__(self, path: pathlib.Path, destination: pathlib.Path, cause: Exception):
        super().__init__(f"Failed to copy {path.name} to {destination}: {cause}")
        self.path = path
        self.destination = destination
        self.cause = cause


# ---------- parsing ----------

class ParseError(IngestionError):
    """Base for all parser failures."""
    def __init__(self, path: pathlib.Path, reason: str, detail: dict[str, Any]):
        super().__init__(f"Parse error in {path.name}: {reason} - {detail}")
        self.path = path
        self.reason = reason
        self.detail = detail


class MalformedBomError(ParseError):
    pass


class MalformedEcoError(ParseError):
    pass


class MalformedTravelerError(ParseError):
    pass


class AnchorNotFound(ParseError):
    """Traveler anchor cell does not contain the expected label."""
    def __init__(self, path: pathlib.Path, field_key: str,
                 expected: str | list[str], observed: Any, anchor_cell: str):
        super().__init__(path, "ANCHOR_NOT_FOUND", {
            "field_key": field_key,
            "expected": expected,
            "observed": observed,
            "anchor_cell": anchor_cell
        })
        self.field_key = field_key
        self.expected = expected
        self.observed = observed
        self.anchor_cell = anchor_cell


class CoercionError(ParseError):
    """Traveler value at expected offset could not be coerced to declared type."""
    def __init__(self, path: pathlib.Path, field_key: str,
                 declared_type: str, observed: Any):
        super().__init__(path, "COERCION_ERROR", {
            "field_key": field_key,
            "declared_type": declared_type,
            "observed": observed
        })
        self.field_key = field_key
        self.declared_type = declared_type
        self.observed = observed


# ---------- coordinate map ----------

class CoordinateMapError(IngestionError):
    """Default or user-supplied traveler_map.json is invalid."""
    def __init__(self, source: pathlib.Path | Literal["packaged-default"],
                 reason: str, detail: dict[str, Any]):
        src_str = str(source) if isinstance(source, pathlib.Path) else source
        super().__init__(f"Invalid coordinate map ({src_str}): {reason} - {detail}")
        self.source = source
        self.reason = reason
        self.detail = detail


# ---------- cross-validation ----------

class CrossValidationError(IngestionError):
    """Two or more parser results disagree on a value that must be consistent."""
    def __init__(self, reason: str, observed: dict[str, Any]):
        super().__init__(f"Cross-validation failed: {reason} - {observed}")
        self.reason = reason
        self.observed = observed
