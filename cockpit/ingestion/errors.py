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


class StoredFileVerificationError(IngestionError):
    """The bytes at the storage destination do not hash to the expected value.

    Raised before any database mutation. The registered file_hash is what the
    startup orphan sweep matches on, so a row whose hash disagrees with its
    bytes gets the file unlinked underneath it.
    """
    def __init__(self, path: pathlib.Path, destination: pathlib.Path):
        super().__init__(
            f"Stored copy of {path.name} at {destination} does not match its "
            f"expected content; the file was not registered."
        )
        self.path = path
        self.destination = destination


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


class MalformedPdfError(ParseError):
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


# ---------- fetch / locator ----------

class SourceRootNotConfigured(IngestionError):
    def __init__(self):
        super().__init__("Source root is not configured.")


class SourceRootMalformed(IngestionError):
    def __init__(self, stored_value: str):
        super().__init__(f"Source root is malformed: {stored_value}")
        self.stored_value = stored_value


class SourceRootUnreachable(IngestionError):
    def __init__(self, root: pathlib.Path, cause: Exception):
        super().__init__(f"Source root unreachable: {root} - {cause}")
        self.root = root
        self.cause = cause


class JobNumberMalformed(IngestionError):
    def __init__(self, raw: str):
        super().__init__(f"Job number malformed: {raw}")
        self.raw = raw


class JobDirectoryNotFound(IngestionError):
    def __init__(self, job_number: str, resolved_path: pathlib.Path):
        super().__init__(f"Job directory not found for {job_number}: {resolved_path}")
        self.job_number = job_number
        self.resolved_path = resolved_path


class JobDirectoryEscapesRoot(IngestionError):
    def __init__(self, job_number: str, resolved_path: pathlib.Path, root: pathlib.Path):
        super().__init__(f"Job directory escapes root for {job_number}: {resolved_path}")
        self.job_number = job_number
        self.resolved_path = resolved_path
        self.root = root


class RequiredRoleMissing(IngestionError):
    def __init__(self, job_number: str, missing_roles: list[str], present_file_names: list[str]):
        super().__init__(f"Required role missing for {job_number}: {missing_roles}")
        self.job_number = job_number
        self.missing_roles = missing_roles
        self.present_file_names = present_file_names


class JobNumberMismatch(IngestionError):
    def __init__(self, job_number: str, bom_file_name: str, derived_token: str):
        super().__init__(f"Job number mismatch: {job_number} != {derived_token} in {bom_file_name}")
        self.job_number = job_number
        self.bom_file_name = bom_file_name
        self.derived_token = derived_token


class FetchTimedOut(IngestionError):
    def __init__(self, root: pathlib.Path, elapsed_ms: int):
        super().__init__(f"Fetch timed out waiting on {root} after {elapsed_ms}ms")
        self.root = root
        self.elapsed_ms = elapsed_ms

