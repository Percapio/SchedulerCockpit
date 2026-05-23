"""Ingestion progress reporting and cancellation."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProgressStage(StrEnum):
    """Named pipeline stages."""
    GATEKEEPER_PASSED   = "gatekeeper_passed"
    FILES_CATEGORIZED   = "files_categorized"
    FILES_HASHED        = "files_hashed"
    FILES_COPIED        = "files_copied"
    PDF_HASHED          = "pdf_hashed"
    PDF_COPIED          = "pdf_copied"
    BOM_PARSED          = "bom_parsed"
    ECO_PARSED          = "eco_parsed"
    TRAVELER_PARSED     = "traveler_parsed"
    PDF_PARSED          = "pdf_parsed"
    CROSS_VALIDATED     = "cross_validated"
    PERSISTED           = "persisted"


@dataclass(frozen=True)
class ProgressEvent:
    stage: ProgressStage
    detail: dict[str, Any] | None = None


class IngestionCancelled(Exception):
    """Raised by the progress callback when the operator requests cancellation.
    
    Inheritance: direct subclass of Exception (NOT of IngestionError).
    Must NOT be passed to error_messages.render.
    """
    pass
