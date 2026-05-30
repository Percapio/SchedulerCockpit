"""Exception-to-payload mapping for the UI."""

import logging
from dataclasses import dataclass

from cockpit.persistence.errors import (
    DuplicateIdentityError, PersistenceUnavailable, SchemaInitializationError, SchemaMismatch,
    IncompleteChecklistError, IllegalStateTransition
)
from cockpit.ingestion.errors import (
    AnchorNotFound, CategorizationError, CoercionError, CoordinateMapError, CrossValidationError,
    FileStorageError, GatekeeperViolation, HashingError, MalformedBomError, MalformedEcoError,
    MalformedTravelerError
)
from cockpit.services.completion import CleanupFailedError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailurePayload:
    exception_class: str
    title: str
    summary: str
    detail: list[tuple[str, str]]
    reason_code: str | None


def render(exc: Exception) -> FailurePayload:
    """Turn a Phase 1 / Phase 2 exception into a FailurePayload."""
    
    exc_class = exc.__class__.__name__

    if isinstance(exc, GatekeeperViolation):
        return FailurePayload(
            exception_class=exc_class,
            title="Files don't match",
            summary="The dropped files do not meet the ingestion requirements.",
            detail=[(k, str(v)) for k, v in exc.detail.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, CategorizationError):
        return FailurePayload(
            exception_class=exc_class,
            title="Internal file classification error",
            summary="An internal error occurred classifying the files. Please escalate to development.",
            detail=[("path", str(exc.path))],
            reason_code=exc.reason
        )

    if isinstance(exc, HashingError):
        return FailurePayload(
            exception_class=exc_class,
            title="Could not read file",
            summary="Failed to read a file for hashing.",
            detail=[("path", str(exc.path)), ("cause", exc.cause.__class__.__name__)],
            reason_code=None
        )

    if isinstance(exc, FileStorageError):
        return FailurePayload(
            exception_class=exc_class,
            title="Could not save uploaded file",
            summary="Failed to copy an uploaded file to internal storage.",
            detail=[
                ("source", str(exc.path)),
                ("destination", str(exc.destination)),
                ("cause", exc.cause.__class__.__name__)
            ],
            reason_code=None
        )

    if isinstance(exc, MalformedBomError):
        if exc.reason == "MISSING_FIND_NUMBER":
            title = "Missing Find#"
            summary = "Audit BOM row is missing a Find# value. Every BOM line must have a Find# number."
        elif exc.reason == "INVALID_FIND_NUMBER":
            title = "Invalid Find#"
            summary = "Audit BOM Find# value is not a whole number. Find# must be an integer line number."
        else:
            title = "Audit BOM is malformed"
            summary = "The Audit BOM file structure does not match expectations."
            
        return FailurePayload(
            exception_class=exc_class,
            title=title,
            summary=summary,
            detail=[(k, str(v)) for k, v in exc.detail.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, MalformedEcoError):
        return FailurePayload(
            exception_class=exc_class,
            title="ECO document is malformed",
            summary="The ECO/Build Notes file structure does not match expectations.",
            detail=[(k, str(v)) for k, v in exc.detail.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, MalformedTravelerError):
        return FailurePayload(
            exception_class=exc_class,
            title="Traveler is malformed",
            summary="The Traveler file structure does not match expectations.",
            detail=[(k, str(v)) for k, v in exc.detail.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, AnchorNotFound):
        return FailurePayload(
            exception_class=exc_class,
            title="Traveler template has shifted",
            summary="Could not find an expected label in the traveler document.",
            detail=[
                ("field_key", str(exc.field_key)),
                ("anchor_cell", str(exc.anchor_cell)),
                ("expected", str(exc.expected)),
                ("observed", str(exc.observed))
            ],
            reason_code="ANCHOR_NOT_FOUND"
        )

    if isinstance(exc, CoercionError):
        return FailurePayload(
            exception_class=exc_class,
            title="Traveler value is wrong type",
            summary="A value in the traveler could not be converted to the required type.",
            detail=[
                ("field_key", str(exc.field_key)),
                ("declared_type", str(exc.declared_type)),
                ("observed", str(exc.observed))
            ],
            reason_code="COERCION_ERROR"
        )

    if isinstance(exc, CoordinateMapError):
        return FailurePayload(
            exception_class=exc_class,
            title="Coordinate map is invalid",
            summary="The traveler coordinate map configuration is invalid.",
            detail=[("source", str(exc.source))] + [(k, str(v)) for k, v in exc.detail.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, CrossValidationError):
        return FailurePayload(
            exception_class=exc_class,
            title="Files disagree",
            summary="The ingested files contain conflicting identity information.",
            detail=[(k, str(v)) for k, v in exc.observed.items()],
            reason_code=exc.reason
        )

    if isinstance(exc, DuplicateIdentityError):
        return FailurePayload(
            exception_class=exc_class,
            title="Audit already exists",
            summary="An active audit for this assembly already exists.",
            detail=[
                ("part_number", exc.part_number),
                ("work_order_ref", exc.work_order_ref),
                ("split_suffix", exc.split_suffix)
            ],
            reason_code="DUPLICATE_IDENTITY"
        )

    if isinstance(exc, PersistenceUnavailable):
        return FailurePayload(
            exception_class=exc_class,
            title="Database is unavailable",
            summary="Could not open the local database.",
            detail=[
                ("db_path", str(exc.db_path)),
                ("cause", exc.cause.__class__.__name__)
            ],
            reason_code="PERSISTENCE_UNAVAILABLE"
        )

    if isinstance(exc, SchemaInitializationError):
        stmt_trunc = exc.statement[:100] + "..." if len(exc.statement) > 100 else exc.statement
        return FailurePayload(
            exception_class=exc_class,
            title="Database setup failed",
            summary="Failed to initialize the database schema.",
            detail=[
                ("statement", stmt_trunc),
                ("cause", exc.cause.__class__.__name__)
            ],
            reason_code="SCHEMA_INITIALIZATION_ERROR"
        )

    if isinstance(exc, SchemaMismatch):
        return FailurePayload(
            exception_class=exc_class,
            title="Database is from a newer release",
            summary="The database schema is too new for this version of Cockpit.",
            detail=[
                ("found_version", str(exc.found_version)),
                ("expected_version", str(exc.expected_version))
            ],
            reason_code="SCHEMA_MISMATCH"
        )

    if isinstance(exc, IncompleteChecklistError):
        return FailurePayload(
            exception_class=exc_class,
            title="Internal verification state error",
            summary="Please escalate to development.",
            detail=[
                ("tht_unverified", str(exc.tht_unverified)),
                ("notes_unverified", str(exc.notes_unverified))
            ],
            reason_code="INCOMPLETE_CHECKLIST"
        )

    if isinstance(exc, IllegalStateTransition):
        return FailurePayload(
            exception_class=exc_class,
            title="Cannot change audit at this stage",
            summary="Please refresh and re-select.",
            detail=[
                ("from_status", str(exc.from_status)),
                ("to_status", str(exc.to_status))
            ],
            reason_code="ILLEGAL_STATE_TRANSITION"
        )

    if isinstance(exc, CleanupFailedError):
        detail = [("audit_id", str(exc.audit_id))]
        for path, reason in exc.reap_report.failed_paths:
            detail.append((str(path), reason))
        
        return FailurePayload(
            exception_class=exc_class,
            title="Audit completed, but file cleanup failed",
            summary="The database was updated successfully, but some physical files could not be deleted. The system will retry deleting them on the next application startup.",
            detail=detail,
            reason_code="CLEANUP_FAILED"
        )

    # Catch-all
    logger.error("Unexpected error during ingestion", exc_info=exc)
    return FailurePayload(
        exception_class=exc_class,
        title="Unexpected error",
        summary="An unknown error occurred.",
        detail=[("error", str(exc))],
        reason_code=None
    )
