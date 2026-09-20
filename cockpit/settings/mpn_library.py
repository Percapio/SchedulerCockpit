"""MPN Library settings (Phase 47 section 10.1, Phase 48 section 3).

This controller is the only reader and the only writer of any ``library/*``
key. No widget constructs a settings object of its own: the store is injected
at ``cockpit/ui/app.py`` and reaches every consumer by injection. A widget that
constructs a settings object with no arguments gets no backing store at all,
which is the defect Phase 48 section 2 repairs.
"""

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

from PyQt6.QtCore import QObject, QSettings, pyqtSignal

_CLIENT_ID_KEY = "library/digikey_client_id"
_CLIENT_SECRET_KEY = "library/digikey_client_secret"
_API_BASE_URL_KEY = "library/api_base_url"
_CALL_CEILING_KEY = "library/call_ceiling"
_ENABLED_KEY = "library/enabled"
_STALE_AFTER_DAYS_KEY = "library/stale_after_days"



class MissingCredentialField(Enum):
    CLIENT_ID = "CLIENT_ID"
    CLIENT_SECRET = "CLIENT_SECRET"


@dataclass(frozen=True)
class Configured:
    """Both credential values are stored and non-empty after trimming."""
    credentials: object  # DigiKeyCredentials


@dataclass(frozen=True)
class Partial:
    """Exactly one credential value is stored. Carries no values."""
    missing: MissingCredentialField


@dataclass(frozen=True)
class Unset:
    """Neither credential value is stored."""


StoredCredentials = Configured | Partial | Unset


class BaseUrlFault(Enum):
    MALFORMED_URL = "MALFORMED_URL"
    NON_HTTPS_SCHEME = "NON_HTTPS_SCHEME"
    USERINFO_PRESENT = "USERINFO_PRESENT"
    NO_HOST = "NO_HOST"
    QUERY_OR_FRAGMENT_PRESENT = "QUERY_OR_FRAGMENT_PRESENT"


BASE_URL_FAULT_TEXT = {
    BaseUrlFault.MALFORMED_URL: "API host is not an absolute URL.",
    BaseUrlFault.NON_HTTPS_SCHEME: "API host must use https.",
    BaseUrlFault.USERINFO_PRESENT: "API host must not carry a username or password.",
    BaseUrlFault.NO_HOST: "API host names no host.",
    BaseUrlFault.QUERY_OR_FRAGMENT_PRESENT: "API host must not carry a query string or fragment.",
}


@dataclass(frozen=True)
class Accepted:
    """An absolute https URL with a host, no userinfo, no query, no fragment."""
    url: str


@dataclass(frozen=True)
class Rejected:
    """The first fault that applies, in the order BaseUrlFault declares."""
    fault: BaseUrlFault


ValidatedBaseUrl = Accepted | Rejected


def production_api_base_url() -> str:
    from cockpit.services.mpn_library.digikey_gateway import PRODUCTION_API_BASE
    return PRODUCTION_API_BASE


def enrichment_call_ceiling_default() -> int:
    from cockpit.services.mpn_library.digikey_gateway import ENRICHMENT_CALL_CEILING_DEFAULT
    return ENRICHMENT_CALL_CEILING_DEFAULT


def min_call_ceiling() -> int:
    """Smallest ceiling under which one product query survives a full retry
    sequence: one token mint, one request, and up to MAX_RETRIES re-mints."""
    from cockpit.services.mpn_library.digikey_gateway import MAX_RETRIES
    return MAX_RETRIES + 2


def validate_api_base_url(raw: str) -> ValidatedBaseUrl:
    """Validates a candidate API base, returning the first fault that applies.

    Faults are evaluated in the order BaseUrlFault declares them, so a value
    violating two rules has one correct answer.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return Rejected(BaseUrlFault.MALFORMED_URL)

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return Rejected(BaseUrlFault.MALFORMED_URL)

    if not parts.scheme:
        return Rejected(BaseUrlFault.MALFORMED_URL)
    if parts.scheme.lower() != "https":
        return Rejected(BaseUrlFault.NON_HTTPS_SCHEME)
    if "@" in parts.netloc:
        return Rejected(BaseUrlFault.USERINFO_PRESENT)

    try:
        hostname = parts.hostname
    except ValueError:
        return Rejected(BaseUrlFault.MALFORMED_URL)
    if not hostname:
        return Rejected(BaseUrlFault.NO_HOST)

    if parts.query or parts.fragment:
        return Rejected(BaseUrlFault.QUERY_OR_FRAGMENT_PRESENT)

    return Accepted(candidate.rstrip("/"))


class MpnLibrarySettingsController(QObject):
    changed = pyqtSignal()

    def __init__(self, settings: QSettings):
        super().__init__()
        self._settings = settings

    def is_enabled(self) -> bool:
        # Default is False
        val = self._settings.value(_ENABLED_KEY, False, type=bool)
        return val

    def set_enabled(self, enabled: bool):
        self._settings.setValue(_ENABLED_KEY, enabled)
        self.changed.emit()

    def stale_after_days(self) -> int:
        # Default is 180
        val = self._settings.value(_STALE_AFTER_DAYS_KEY, 180, type=int)
        return val

    def set_stale_after_days(self, days: int):
        self._settings.setValue(_STALE_AFTER_DAYS_KEY, days)
        self.changed.emit()

    # --- credentials -----------------------------------------------------

    def _stored_credential_text(self) -> tuple[str, str]:
        client_id = self._settings.value(_CLIENT_ID_KEY, "", type=str) or ""
        client_secret = self._settings.value(_CLIENT_SECRET_KEY, "", type=str) or ""
        return client_id.strip(), client_secret.strip()

    def digikey_credentials(self) -> StoredCredentials:
        """The stored DigiKey credentials, or the reason there are none usable.

        Trimming is not cosmetic: a client id pasted from a web console carries
        a trailing newline often enough that an untrimmed comparison against ""
        passes while the token endpoint rejects the value, and the operator is
        told their credentials are wrong when they are merely dirty.
        """
        client_id, client_secret = self._stored_credential_text()

        if client_id and client_secret:
            from cockpit.services.mpn_library.digikey_types import DigiKeyCredentials
            return Configured(DigiKeyCredentials(client_id=client_id, client_secret=client_secret))
        if client_id:
            return Partial(MissingCredentialField.CLIENT_SECRET)
        if client_secret:
            return Partial(MissingCredentialField.CLIENT_ID)
        return Unset()

    def stored_client_id(self) -> str:
        """The stored client id, trimmed. Not a secret: DigiKey requires it in
        a request header, so rendering it back into a widget reveals nothing."""
        return self._stored_credential_text()[0]

    def has_stored_secret(self) -> bool:
        """Whether a secret is stored, without returning it.

        The settings dialog needs this to decide whether a blank secret field
        means "unchanged" or "not yet entered". It never reads the value back.
        """
        _, client_secret = self._stored_credential_text()
        return bool(client_secret)

    def set_digikey_credentials(self, client_id: str, client_secret: str) -> None:
        """Stores both credential values as one unit, emitting changed once."""
        trimmed_id = (client_id or "").strip()
        trimmed_secret = (client_secret or "").strip()
        if not trimmed_id or not trimmed_secret:
            raise ValueError("set_digikey_credentials requires both values")

        self._settings.setValue(_CLIENT_ID_KEY, trimmed_id)
        self._settings.setValue(_CLIENT_SECRET_KEY, trimmed_secret)
        self._settings.sync()
        self.changed.emit()

    def set_digikey_client_id(self, client_id: str) -> None:
        """Replaces the client id, keeping the stored secret byte-identical.

        Exists because the secret field is never populated from storage: an
        operator correcting a typo in the id can only supply the id, and the
        paired writer refuses a blank secret.
        """
        trimmed_id = (client_id or "").strip()
        if not trimmed_id:
            raise ValueError("set_digikey_client_id requires a non-empty value")
        if not self.has_stored_secret():
            raise ValueError("set_digikey_client_id requires a stored secret")

        self._settings.setValue(_CLIENT_ID_KEY, trimmed_id)
        self._settings.sync()
        self.changed.emit()

    def forget_digikey_credentials(self) -> None:
        """Removes both credential keys. The only path that removes them.

        library/enabled and every other library/* key are untouched: an
        operator forgetting their keys keeps their library.
        """
        self._settings.remove(_CLIENT_ID_KEY)
        self._settings.remove(_CLIENT_SECRET_KEY)
        self._settings.sync()
        self.changed.emit()

    # --- API base URL ----------------------------------------------------

    def api_base_url(self) -> ValidatedBaseUrl:
        """The configured API base, or the reason it is unusable.

        A stored value that fails any rule is never returned as Accepted and
        is never silently replaced with the production default: an operator who
        mistyped a sandbox host must not silently spend production quota.
        """
        stored = self._settings.value(_API_BASE_URL_KEY, "", type=str) or ""
        if not stored.strip():
            return validate_api_base_url(production_api_base_url())
        return validate_api_base_url(stored)

    def api_base_url_text(self) -> str:
        """The stored base URL verbatim, for rendering into the edit field."""
        stored = self._settings.value(_API_BASE_URL_KEY, "", type=str) or ""
        return stored.strip() or production_api_base_url()

    def set_api_base_url(self, base_url: str) -> None:
        self._settings.setValue(_API_BASE_URL_KEY, (base_url or "").strip())
        self._settings.sync()
        self.changed.emit()

    # --- call ceiling ----------------------------------------------------

    def call_ceiling(self) -> int:
        """Upper bound on API calls in one enrichment run, token mint included."""
        stored = self._settings.value(
            _CALL_CEILING_KEY, enrichment_call_ceiling_default(), type=int
        )
        return max(int(stored), min_call_ceiling())

    def set_call_ceiling(self, ceiling: int) -> None:
        floor = min_call_ceiling()
        if int(ceiling) < floor:
            raise ValueError(f"call ceiling must be at least {floor}")
        self._settings.setValue(_CALL_CEILING_KEY, int(ceiling))
        self._settings.sync()
        self.changed.emit()
