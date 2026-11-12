"""Credential proxy: the only process that holds the real model-API credential.

Worker containers run on an internal Docker network with a placeholder credential
and `ANTHROPIC_BASE_URL` pointing here (doc/architecture.md §10). This proxy forwards
`POST /v1/messages` (and its `count_tokens`) to the real API with the placeholder auth
header replaced by the real one, refuses every other request, and streams responses unbuffered. So a
prompt-injected worker can neither read the credential nor reach any other host
(invariants 10 and 13). With a token cap, it meters the usage every model response
reports and refuses new requests once the cap is spent, so a compromised worker
cannot burn quota without limit.

Stdlib only and free of SEAM imports: the CLI mounts this one file read-only into a
container and runs it as `python3 proxy.py`.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import math
import os
import sys
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

DEFAULT_UPSTREAM = "https://api.anthropic.com"
HEALTH_PATH = "/seam-proxy/health"  # answered locally; never forwarded
# Generation, and token counting (Claude Code sizes its context with it; it generates
# nothing and is not billed). Everything else is refused.
ALLOWED = {("POST", "/v1/messages"), ("POST", "/v1/messages/count_tokens")}

_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailer", "transfer-encoding", "upgrade", "host", "content-length",
}
_AUTH_HEADERS = {"authorization", "x-api-key"}
# Dropped so responses come back uncompressed and their usage can be metered.
_UNMETERABLE = {"accept-encoding"}
GENERATE_PATH = "/v1/messages"  # the only billed endpoint; count_tokens is free
CACHE_READ_WEIGHT = 0.1  # cache reads cost a tenth of an input token


def metered_tokens(usage: dict) -> int:
    """Tokens charged against the cap for one response's `usage`: uncached input,
    cache writes and output in full, cache reads at their price ratio (a tenth)."""
    def n(key: str) -> int:
        v = usage.get(key)
        return v if isinstance(v, int) and v > 0 else 0
    return (n("input_tokens") + n("cache_creation_input_tokens") + n("output_tokens")
            + math.ceil(n("cache_read_input_tokens") * CACHE_READ_WEIGHT))


def response_usage(body: bytes) -> dict | None:
    """The final `usage` of a Messages response, plain JSON or an SSE stream (where
    `message_start` carries it and each `message_delta` updates it cumulatively).
    None when the body reports no usage (an error, or an unparseable body)."""
    text = body.decode("utf-8", "replace")
    if text.lstrip().startswith("{"):
        try:
            usage = json.loads(text).get("usage")
        except (ValueError, AttributeError):
            return None
        return usage if isinstance(usage, dict) else None
    usage: dict | None = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "message_start":
            u = (event.get("message") or {}).get("usage")
            usage = dict(u) if isinstance(u, dict) else usage
        elif event.get("type") == "message_delta" and isinstance(event.get("usage"), dict):
            usage = {**(usage or {}), **event["usage"]}
    return usage


class TokenMeter:
    """Thread-safe running total against a cap (None = unlimited). Requests are
    checked before they are forwarded and charged when their response ends, so the
    cap is overshot by at most the requests already in flight when it is reached."""

    def __init__(self, cap: int | None = None) -> None:
        self.cap = cap
        self.spent = 0
        self._lock = threading.Lock()

    def exhausted(self) -> bool:
        with self._lock:
            return self.cap is not None and self.spent >= self.cap

    def charge(self, tokens: int) -> int:
        with self._lock:
            self.spent += tokens
            return self.spent


@dataclass(frozen=True)
class CredentialKind:
    """How one credential env var is presented to the API, and its placeholder."""

    env_name: str
    header: str
    template: str
    placeholder: str  # what workers get instead; has the real kind's shape

    def header_value(self, secret: str) -> str:
        return self.template.format(secret)


CREDENTIAL_KINDS = {
    k.env_name: k
    for k in (
        CredentialKind("CLAUDE_CODE_OAUTH_TOKEN", "Authorization", "Bearer {}",
                       "sk-ant-oat01-seam-proxy-placeholder"),
        CredentialKind("ANTHROPIC_API_KEY", "x-api-key", "{}",
                       "sk-ant-api03-seam-proxy-placeholder"),
    )
}


def make_handler(kind: CredentialKind, secret: str, upstream: str,
                 allow: list[ipaddress.IPv4Network | ipaddress.IPv6Network] | None = None,
                 timeout: float = 600.0,
                 meter: TokenMeter | None = None) -> type[BaseHTTPRequestHandler]:
    """Build the request handler. `allow`: client networks served (None = any);
    `meter`: the token cap shared by every request this proxy serves."""
    meter = meter or TokenMeter()
    up = urlsplit(upstream)
    conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
    real_auth = kind.header_value(secret)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0: the response body ends at connection close, so an upstream
        # chunked stream (SSE) can be relayed as-is without re-chunking.
        protocol_version = "HTTP/1.0"

        def _refuse(self, code: int, why: str, error: str = "permission_error") -> None:
            self.log_message("refused %s %s: %s", self.command, self.path, why)
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"type": "error", "error": {
                "type": error,
                "message": f"refused by SEAM credential proxy: {why}"}}).encode())

        def _client_allowed(self) -> bool:
            if allow is None:
                return True
            addr = ipaddress.ip_address(self.client_address[0])
            return any(addr in net for net in allow)

        def _handle(self) -> None:
            if not self._client_allowed():
                return self._refuse(403, f"client {self.client_address[0]} not allowed")
            path = urlsplit(self.path).path
            if self.command == "GET" and path == HEALTH_PATH:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")
                return
            if (self.command, path) not in ALLOWED:
                return self._refuse(403, "not an allowed endpoint")
            if "transfer-encoding" in {k.lower() for k in self.headers}:
                return self._refuse(411, "chunked request bodies are not supported")
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            billed = path == GENERATE_PATH
            if billed and meter.exhausted():
                return self._refuse(429, f"token cap {meter.cap} reached", "rate_limit_error")

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP | _AUTH_HEADERS | _UNMETERABLE}
            headers[kind.header] = real_auth
            conn = conn_cls(up.netloc, timeout=timeout)
            try:
                conn.request("POST", self.path, body=body, headers=headers)
                resp = conn.getresponse()
                self.send_response(resp.status, resp.reason)
                for k, v in resp.getheaders():
                    if k.lower() not in _HOP_BY_HOP:
                        self.send_header(k, v)
                self.end_headers()
                relayed = bytearray()
                try:
                    while chunk := resp.read1(65536):
                        relayed += chunk
                        self.wfile.write(chunk)
                        self.wfile.flush()
                finally:  # a client that hangs up mid-stream is still charged
                    if billed:
                        self._charge(bytes(relayed))
            except OSError as e:
                self.log_message("upstream error: %s", type(e).__name__)
                if not self.wfile.closed:
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
            finally:
                conn.close()

        def _charge(self, relayed: bytes) -> None:
            usage = response_usage(relayed)
            if usage is None:
                self.log_message("no usage in response to %s", self.path)
                return
            used = metered_tokens(usage)
            total = meter.charge(used)
            self.log_message("metered %d tokens (total %d of %s)", used, total,
                             meter.cap if meter.cap is not None else "no cap")

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle

        def log_message(self, fmt: str, *args: object) -> None:
            # Request lines and refusals only; headers (which hold the credential) and
            # bodies are never logged.
            sys.stderr.write("seam-proxy: " + (fmt % args) + "\n")

    return Handler


def serve(kind: CredentialKind, secret: str, listen: tuple[str, int],
          upstream: str = DEFAULT_UPSTREAM, allow=None,
          token_cap: int | None = None) -> ThreadingHTTPServer:
    """Create (not start) the proxy server; call `serve_forever()` on the result."""
    meter = TokenMeter(token_cap)
    return ThreadingHTTPServer(listen, make_handler(kind, secret, upstream, allow,
                                                    meter=meter))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--credential-env", required=True, choices=sorted(CREDENTIAL_KINDS))
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    p.add_argument("--allow", action="append", default=None,
                   help="client CIDR to serve (repeatable; default: any)")
    p.add_argument("--token-cap", type=int, default=None,
                   help="refuse generation once this many metered tokens are spent")
    args = p.parse_args(argv)
    secret = os.environ.get(args.credential_env)
    if not secret:
        print(f"seam-proxy: {args.credential_env} is not set", file=sys.stderr)
        return 2
    allow = [ipaddress.ip_network(c) for c in args.allow] if args.allow else None
    server = serve(CREDENTIAL_KINDS[args.credential_env], secret,
                   ("0.0.0.0", args.port), args.upstream, allow, args.token_cap)
    print(f"seam-proxy: listening on :{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
