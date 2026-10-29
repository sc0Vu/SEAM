"""The credential proxy against a fake upstream on localhost (no network, no model)."""

import http.client
import ipaddress
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sandbox.proxy import CREDENTIAL_KINDS, HEALTH_PATH, serve

OAUTH = CREDENTIAL_KINDS["CLAUDE_CODE_OAUTH_TOKEN"]
API_KEY = CREDENTIAL_KINDS["ANTHROPIC_API_KEY"]
SECRET = "real-secret-value"


class Upstream:
    """Records requests; `/stream` sends one chunk, waits for `release`, sends more."""

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.release = threading.Event()
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                up.requests.append((self.command, self.path, dict(self.headers), body))
                if "stream" in self.path:
                    self.send_response(200)
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for part in (b"event: one\n\n", None, b"event: two\n\n"):
                        if part is None:
                            up.release.wait(5)
                            continue
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(part), part))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    return
                status = 429 if "busy" in self.path else 200
                out = b'{"ok": true}'
                self.send_response(status)
                self.send_header("content-length", str(len(out)))
                self.send_header("request-id", "req_1")
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()


@pytest.fixture
def upstream():
    up = Upstream()
    yield up
    up.release.set()
    up.server.shutdown()


def start_proxy(upstream, kind=OAUTH, allow=None):
    server = serve(kind, SECRET, ("127.0.0.1", 0), upstream.url, allow)
    threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True).start()
    return server


def call(proxy, method, path, headers=None, body=b""):
    conn = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    return conn.getresponse()


def test_oauth_placeholder_is_swapped_for_the_real_token(upstream):
    proxy = start_proxy(upstream)
    resp = call(proxy, "POST", "/v1/messages?beta=true", body=b'{"m": 1}',
                headers={"Authorization": f"Bearer {OAUTH.placeholder}",
                         "anthropic-beta": "oauth-2025-04-20"})
    assert (resp.status, resp.read()) == (200, b'{"ok": true}')
    assert resp.getheader("request-id") == "req_1"
    method, path, headers, body = upstream.requests[0]
    assert (method, path, body) == ("POST", "/v1/messages?beta=true", b'{"m": 1}')
    assert headers["Authorization"] == f"Bearer {SECRET}"
    assert headers["anthropic-beta"] == "oauth-2025-04-20"  # other headers pass through
    assert OAUTH.placeholder not in str(headers)
    proxy.shutdown()


def test_api_key_mode_sets_x_api_key_and_drops_other_auth(upstream):
    proxy = start_proxy(upstream, kind=API_KEY)
    call(proxy, "POST", "/v1/messages", body=b"{}",
         headers={"x-api-key": API_KEY.placeholder, "Authorization": "Bearer stray"}).read()
    headers = {k.lower(): v for k, v in upstream.requests[0][2].items()}
    assert headers["x-api-key"] == SECRET
    assert "authorization" not in headers
    proxy.shutdown()


@pytest.mark.parametrize("method, path", [
    ("GET", "/v1/messages"),
    ("POST", "/v1/files"),
    ("POST", "/v1/messages/../files"),
    ("GET", "/v1/models"),
    ("POST", "/api/oauth/profile"),
])
def test_everything_but_post_messages_is_refused_and_never_forwarded(upstream, method, path):
    proxy = start_proxy(upstream)
    resp = call(proxy, method, path)
    assert resp.status == 403 and b"SEAM credential proxy" in resp.read()
    assert upstream.requests == []
    proxy.shutdown()


def test_token_counting_is_forwarded(upstream):
    proxy = start_proxy(upstream)
    assert call(proxy, "POST", "/v1/messages/count_tokens?beta=true", body=b"{}").status == 200
    assert upstream.requests[0][1] == "/v1/messages/count_tokens?beta=true"
    proxy.shutdown()


def test_health_is_answered_locally(upstream):
    proxy = start_proxy(upstream)
    resp = call(proxy, "GET", HEALTH_PATH)
    assert (resp.status, resp.read()) == (200, b"ok")
    assert upstream.requests == []
    proxy.shutdown()


def test_upstream_errors_pass_through(upstream):
    proxy = start_proxy(upstream)
    resp = call(proxy, "POST", "/v1/messages?busy=1", body=b"{}")
    assert resp.status == 429
    proxy.shutdown()


def test_stream_is_relayed_before_upstream_finishes(upstream):
    proxy = start_proxy(upstream)
    resp = call(proxy, "POST", "/v1/messages?stream=1", body=b"{}")
    assert resp.status == 200
    # The upstream is blocked until we release it, so this read proves no buffering.
    assert resp.read1(64) == b"event: one\n\n"
    upstream.release.set()
    assert resp.read() == b"event: two\n\n"
    proxy.shutdown()


def test_clients_outside_the_allowed_networks_are_refused(upstream):
    proxy = start_proxy(upstream, allow=[ipaddress.ip_network("10.99.0.0/16")])
    resp = call(proxy, "POST", "/v1/messages", body=b"{}")
    assert resp.status == 403
    assert upstream.requests == []
    proxy.shutdown()
