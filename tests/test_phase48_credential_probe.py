"""Phase 48 section 12.4 -- the credential probe against a stub token endpoint.

The probe exists to give an operator a diagnostic before they spend product
quota, so every outcome it can report is exercised here, and so is the promise
that reporting them costs one request and no quota row.
"""

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cockpit.services.mpn_library import digikey_gateway
from cockpit.services.mpn_library.credential_probe import (
    PROBE_RESULT_TEXT,
    CredentialProbeResult,
    probe_digikey_credentials,
)
from cockpit.services.mpn_library.digikey_types import DigiKeyCredentials
from cockpit.persistence.clock import utcnow

IMPROBABLE_SECRET = "zzq-secret-8f3a1c07-never-rendered"
CREDENTIALS = DigiKeyCredentials(client_id="client-abc", client_secret=IMPROBABLE_SECRET)


class _RequestLog:
    def __init__(self):
        self.paths = []


def _serve(status: int, body: bytes, log: _RequestLog):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            log.paths.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def token_endpoint():
    """Factory yielding (base_url, request_log) for a stub answering `status`."""
    started = []

    def build(status: int, payload: dict | None = None):
        body = json.dumps(payload if payload is not None else {"error": "nope"}).encode()
        log = _RequestLog()
        server, thread = _serve(status, body, log)
        started.append((server, thread))
        host, port = server.server_address
        return f"http://{host}:{port}", log

    yield build

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_reachable_on_200(token_endpoint):
    base_url, log = token_endpoint(200, {"access_token": "tok", "expires_in": 3600})
    assert probe_digikey_credentials(CREDENTIALS, base_url, utcnow) == CredentialProbeResult.REACHABLE
    assert log.paths == ["/v1/oauth2/token"]


def test_credentials_rejected_on_401(token_endpoint):
    base_url, log = token_endpoint(401)
    result = probe_digikey_credentials(CREDENTIALS, base_url, utcnow)
    assert result == CredentialProbeResult.CREDENTIALS_REJECTED
    assert len(log.paths) == 1


def test_rate_limited_on_429(token_endpoint):
    base_url, log = token_endpoint(429)
    result = probe_digikey_credentials(CREDENTIALS, base_url, utcnow)
    assert result == CredentialProbeResult.RATE_LIMITED
    assert len(log.paths) == 1


def test_server_error_on_503_issues_exactly_one_request(token_endpoint):
    """A 503 is not retried. The retry loop lives in resolve_mpn, which the
    probe never calls, and rerouting through it would spend product quota."""
    base_url, log = token_endpoint(503)
    result = probe_digikey_credentials(CREDENTIALS, base_url, utcnow)
    assert result == CredentialProbeResult.SERVER_ERROR
    assert len(log.paths) == 1


def test_unreachable_on_connection_error():
    closed_socket = socket.socket()
    closed_socket.bind(("127.0.0.1", 0))
    port = closed_socket.getsockname()[1]
    closed_socket.close()

    result = probe_digikey_credentials(CREDENTIALS, f"http://127.0.0.1:{port}", utcnow)
    assert result == CredentialProbeResult.UNREACHABLE


def test_timed_out_when_endpoint_never_answers(monkeypatch):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    monkeypatch.setattr(digikey_gateway, "CONNECT_TIMEOUT_S", 0.5)
    try:
        result = probe_digikey_credentials(CREDENTIALS, f"http://127.0.0.1:{port}", utcnow)
    finally:
        listener.close()
    assert result == CredentialProbeResult.TIMED_OUT


def test_probe_writes_no_quota_row(tmp_path, token_endpoint):
    from cockpit.services.mpn_library.module import MpnLibraryModule

    module = MpnLibraryModule(tmp_path / "parts_library.db")
    try:
        for status in (200, 401, 429, 503):
            payload = {"access_token": "tok", "expires_in": 3600} if status == 200 else None
            base_url, _ = token_endpoint(status, payload)
            probe_digikey_credentials(CREDENTIALS, base_url, utcnow)

        closed_socket = socket.socket()
        closed_socket.bind(("127.0.0.1", 0))
        dead_port = closed_socket.getsockname()[1]
        closed_socket.close()
        probe_digikey_credentials(CREDENTIALS, f"http://127.0.0.1:{dead_port}", utcnow)

        rows = module.ui_conn.cursor().execute(
            "SELECT COUNT(*) AS n FROM library_quota_day"
        ).fetchone()
        assert rows["n"] == 0
    finally:
        module.teardown()


def test_no_outcome_renders_the_secret(token_endpoint):
    for outcome in CredentialProbeResult:
        rendered = PROBE_RESULT_TEXT[outcome].format(host="https://api.digikey.com")
        assert IMPROBABLE_SECRET not in rendered
        assert IMPROBABLE_SECRET not in repr(outcome)

    base_url, _ = token_endpoint(401)
    result = probe_digikey_credentials(CREDENTIALS, base_url, utcnow)
    assert IMPROBABLE_SECRET not in repr(result)
    assert IMPROBABLE_SECRET not in PROBE_RESULT_TEXT[result].format(host=base_url)
