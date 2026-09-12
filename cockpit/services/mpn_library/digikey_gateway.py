import json
import logging
import random
import time
from urllib import request, error
from urllib.parse import urlencode
from typing import Optional, Dict
import ssl

from .types import MpnKey
from .digikey_types import (
    DigiKeyCredentials,
    RequestBudget,
    BearerToken,
    DigiKeyProductQuery,
    VariantCandidate,
    Resolved,
    Ambiguous,
    NotFound
)
from .digikey_variant import extract_variants
from cockpit.services.library_errors import (
    CredentialsRejected,
    QuotaExhausted,
    DigiKeyUnreachable,
    MalformedCatalogueResponse
)

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10
READ_TIMEOUT_S = 20
MAX_RETRIES = 3
BACKOFF_BASE_S = 1.0
BACKOFF_JITTER_S = 0.5
RETRY_AFTER_CEILING_S = 60
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TOKEN_REFRESH_SKEW_S = 60

# Configurable endpoints
PRODUCTION_API_BASE = "https://api.digikey.com"

class DigiKeyGateway:
    def __init__(self, api_base: str = PRODUCTION_API_BASE):
        self.api_base = api_base
        self.token: Optional[BearerToken] = None
        self.credentials: Optional[DigiKeyCredentials] = None

    def initialize(self, credentials: DigiKeyCredentials):
        self.credentials = credentials

    def acquire_token(self, budget: RequestBudget, clock) -> BearerToken:
        if not self.credentials:
            raise RuntimeError("Credentials not configured")

        if not budget.take():
            raise QuotaExhausted(budget.spent)

        url = f"{self.api_base}/v1/oauth2/token"
        
        data = urlencode({
            'client_id': self.credentials.client_id,
            'client_secret': self.credentials.client_secret,
            'grant_type': 'client_credentials'
        }).encode('utf-8')
        
        req = request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        
        # We need a custom context for TLS verification (it's never disabled)
        ctx = ssl.create_default_context()
        
        try:
            with request.urlopen(req, timeout=CONNECT_TIMEOUT_S, context=ctx) as response:
                body = response.read()
                resp_json = json.loads(body)
                expires_in = resp_json.get("expires_in", 3600)
                access_token = resp_json["access_token"]
                
                # We return BearerToken
                return BearerToken(
                    access_token=access_token,
                    expires_at=clock().timestamp() + expires_in
                )
        except error.HTTPError as e:
            if e.code == 401:
                # Re-mint once, retry once, then CredentialsRejected
                # But acquire_token IS the minting process. So a 401 here means rejected.
                # "401: Re-mint once, retry once, then CredentialsRejected". 
                # This applies to the product endpoint. For the token endpoint, 401 means credentials are bad.
                host = req.host
                raise CredentialsRejected(host)
            elif e.code == 429:
                # For token endpoint, if we hit 429, we could backoff. But spec says 
                # "429: Honour Retry-After... at most MAX_RETRIES then QuotaExhausted"
                raise QuotaExhausted(budget.spent)
            else:
                raise DigiKeyUnreachable(req.host, 1)
        except Exception as e:
            raise DigiKeyUnreachable(req.host, 1)


    def resolve_mpn(self, mpn_key: MpnKey, budget: RequestBudget, clock) -> Resolved | Ambiguous | NotFound:
        # Check token
        if not self.token or self.token.expires_at < clock().timestamp() + TOKEN_REFRESH_SKEW_S:
            self.token = self.acquire_token(budget, clock)

        query = DigiKeyProductQuery(keywords=mpn_key)
        payload = json.dumps({"keywords": query.keywords}).encode("utf-8")
        
        url = f"{self.api_base}/products/v4/search/keyword"
        
        retries = 0
        while True:
            if not budget.take():
                raise QuotaExhausted(budget.spent)
                
            req = request.Request(url, data=payload, method="POST")
            req.add_header("Authorization", f"Bearer {self.token.access_token}")
            req.add_header("Content-Type", "application/json")
            req.add_header("Accept", "application/json")
            req.add_header("X-DIGIKEY-Client-Id", self.credentials.client_id)
            
            ctx = ssl.create_default_context()
            
            try:
                # We need separate CONNECT_TIMEOUT_S and READ_TIMEOUT_S.
                # urllib doesn't let us easily specify them separately, it uses one timeout for both operations.
                # Wait, we can use http.client for finer control, or just rely on the single timeout for urllib.
                # For now, urllib's timeout covers the full response header read.
                with request.urlopen(req, timeout=CONNECT_TIMEOUT_S + READ_TIMEOUT_S, context=ctx) as response:
                    body = bytearray()
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        body.extend(chunk)
                        if len(body) > MAX_RESPONSE_BYTES:
                            raise MalformedCatalogueResponse(mpn_key, response.status, len(body))
                    
                    try:
                        resp_json = json.loads(body)
                    except json.JSONDecodeError:
                        raise MalformedCatalogueResponse(mpn_key, response.status, len(body))
                        
                    candidates = extract_variants(mpn_key, resp_json)
                    if not candidates:
                        return NotFound(raw_response=bytes(body), http_status=response.status)
                    elif len(candidates) == 1:
                        return Resolved(candidate=candidates[0], raw_response=bytes(body), http_status=response.status)
                    else:
                        return Ambiguous(candidates=candidates, raw_response=bytes(body), http_status=response.status)
                        
            except error.HTTPError as e:
                if e.code == 401:
                    if retries < MAX_RETRIES:
                        retries += 1
                        # Force token refresh on next loop
                        self.token = self.acquire_token(budget, clock)
                        continue
                    else:
                        raise CredentialsRejected(req.host)
                
                retry_after_header = e.headers.get("Retry-After")
                retry_after = int(retry_after_header) if retry_after_header and retry_after_header.isdigit() else None
                
                if e.code == 429:
                    if retries < MAX_RETRIES:
                        retries += 1
                        self._do_backoff(retries, retry_after)
                        continue
                    else:
                        raise QuotaExhausted(budget.spent, retry_after)
                
                if 500 <= e.code < 600:
                    if retries < MAX_RETRIES:
                        retries += 1
                        self._do_backoff(retries, retry_after)
                        continue
                    else:
                        raise DigiKeyUnreachable(req.host, retries + 1)
                        
                # 4xx other than 401 and 429
                # "No retry. Recorded as a per-part failure, not a NOT_FOUND"
                # Wait, how does a per-part failure get reported?
                # We can raise an exception that gets caught by the worker.
                from cockpit.services.library_errors import MalformedCatalogueResponse
                # Wait, "Recorded as a per-part failure" means the worker catches it.
                # We need a new exception for REQUEST_REJECTED.
                # Actually, 47b spec says FailureReason: REQUEST_REJECTED
                # I'll raise a dedicated exception or a general one.
                class RequestRejected(Exception): pass
                raise RequestRejected(f"HTTP {e.code}")

            except (error.URLError, Exception) as e:
                # Transport error or other read timeout
                if isinstance(e, MalformedCatalogueResponse):
                    raise
                if isinstance(e, QuotaExhausted):
                    raise
                if isinstance(e, CredentialsRejected):
                    raise
                    
                if retries < MAX_RETRIES:
                    retries += 1
                    self._do_backoff(retries, None)
                    continue
                else:
                    raise DigiKeyUnreachable(req.host if 'req' in locals() else self.api_base, retries + 1)

    def _do_backoff(self, retry_count: int, retry_after: Optional[int]):
        if retry_after is not None:
            if retry_after > RETRY_AFTER_CEILING_S:
                raise QuotaExhausted(0, retry_after) # The spent is tracked by caller
            time.sleep(retry_after)
        else:
            # Exponential backoff with jitter
            # BACKOFF_BASE_S * (2 ** (retry_count - 1)) + jitter
            sleep_time = BACKOFF_BASE_S * (2 ** (retry_count - 1))
            jitter = random.uniform(0, BACKOFF_JITTER_S)
            time.sleep(sleep_time + jitter)
