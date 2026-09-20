"""Credential probe for the Settings dialog (Phase 48 section 5).

Acquires a token and stops. No product search, so no product quota, and it
proves exactly one thing: these credentials authenticate against this host.
"""

import logging
from enum import Enum
from typing import Callable
from datetime import datetime

from .digikey_types import DigiKeyCredentials, RequestBudget
from cockpit.services.library_errors import (
    CredentialsRejected,
    QuotaExhausted,
    DigiKeyUnreachable,
)

logger = logging.getLogger(__name__)


class CredentialProbeResult(Enum):
    REACHABLE = "REACHABLE"
    CREDENTIALS_REJECTED = "CREDENTIALS_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    UNREACHABLE = "UNREACHABLE"
    TIMED_OUT = "TIMED_OUT"


PROBE_RESULT_TEXT = {
    CredentialProbeResult.REACHABLE: "Credentials accepted by {host}.",
    CredentialProbeResult.CREDENTIALS_REJECTED: "{host} rejected these credentials.",
    CredentialProbeResult.RATE_LIMITED: "{host} is rate limiting. Try again later.",
    CredentialProbeResult.SERVER_ERROR: "{host} returned an error. Credentials were not checked.",
    CredentialProbeResult.UNREACHABLE: "Could not reach {host}.",
    CredentialProbeResult.TIMED_OUT: "{host} did not answer in time.",
}


def probe_digikey_credentials(
    credentials: DigiKeyCredentials,
    api_base: str,
    clock: Callable[[], datetime],
) -> CredentialProbeResult:
    """Proves whether the stored credentials authenticate against api_base.

    Issues exactly one token request and no product request, and does not
    retry: the retry loop lives in resolve_mpn, which this never calls. The
    probe carries its own single-unit budget and writes no library_quota_day
    row, so checking a key never costs the day's product allowance. The minted
    token is discarded rather than retained, so the next enrichment behaves the
    same whether or not this button was pressed.

    Every outcome is a returned value. The client secret appears in no return
    value, no log record and no rendered string.
    """
    from .digikey_gateway import DigiKeyGateway

    gateway = DigiKeyGateway(api_base=api_base)
    gateway.initialize(credentials)

    try:
        gateway.acquire_token(RequestBudget(remaining=1), clock)
        return CredentialProbeResult.REACHABLE
    except CredentialsRejected:
        return CredentialProbeResult.CREDENTIALS_REJECTED
    except QuotaExhausted:
        # The budget starts at one and the first take succeeds, so this can
        # only be the token endpoint answering 429.
        return CredentialProbeResult.RATE_LIMITED
    except DigiKeyUnreachable as unreachable:
        if unreachable.http_status is not None:
            return CredentialProbeResult.SERVER_ERROR
        if unreachable.timed_out:
            return CredentialProbeResult.TIMED_OUT
        return CredentialProbeResult.UNREACHABLE
    except Exception:
        logger.exception("Credential probe failed unexpectedly")
        return CredentialProbeResult.UNREACHABLE
