from pathlib import Path
from typing import Optional, Type

class LibraryUnavailable(Exception):
    """Raised when parts_library.db cannot be created, opened, or migrated."""
    def __init__(self, path: Path, cause_class: Optional[Type[Exception]] = None):
        super().__init__(f"MPN Library unavailable at {path}")
        self.path = path
        self.cause_class = cause_class

class LibraryDisabled(Exception):
    """Raised when a library operation is requested while the toggle is off."""
    pass

class UnknownAttributeName(Exception):
    """Raised when a name not in ATTRIBUTE_REGISTRY reaches the repository."""
    def __init__(self, attribute_name: str):
        super().__init__(f"Unknown library attribute name: {attribute_name}")
        self.attribute_name = attribute_name

class CredentialsNotConfigured(Exception):
    """Raised when enrichment is requested with no client id or secret stored."""
    pass

class CredentialsRejected(Exception):
    """Raised when the token endpoint returns 401."""
    def __init__(self, endpoint_host: str):
        super().__init__(f"Credentials rejected by {endpoint_host}")
        self.endpoint_host = endpoint_host

class QuotaExhausted(Exception):
    """Raised when 429 survives MAX_RETRIES, or RequestBudget reaches zero."""
    def __init__(self, requests_spent: int, retry_after_seconds: Optional[int] = None):
        super().__init__("DigiKey API quota exhausted")
        self.requests_spent = requests_spent
        self.retry_after_seconds = retry_after_seconds

class DigiKeyUnreachable(Exception):
    """Raised when transport failure or 5xx survives MAX_RETRIES."""
    def __init__(self, endpoint_host: str, attempt_count: int):
        super().__init__(f"DigiKey unreachable at {endpoint_host} after {attempt_count} attempts")
        self.endpoint_host = endpoint_host
        self.attempt_count = attempt_count

class EnrichmentTimedOut(Exception):
    """Raised when ENRICHMENT_WATCHDOG_TIMEOUT_MS elapses."""
    def __init__(self, elapsed_ms: int, parts_completed: int):
        super().__init__(f"Enrichment timed out after {elapsed_ms}ms")
        self.elapsed_ms = elapsed_ms
        self.parts_completed = parts_completed

class MalformedCatalogueResponse(Exception):
    """Raised when response is not JSON, wrong shape, or over MAX_RESPONSE_BYTES."""
    def __init__(self, mpn_key: str, http_status: int, byte_count: int):
        super().__init__(f"Malformed response for {mpn_key} ({byte_count} bytes, status {http_status})")
        self.mpn_key = mpn_key
        self.http_status = http_status
        self.byte_count = byte_count
