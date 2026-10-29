"""Credential proxy: the only process that holds the real model-API credential.

Worker containers run on an internal Docker network with a placeholder credential
and `ANTHROPIC_BASE_URL` pointing here (doc/architecture.md §10). This proxy forwards
`POST /v1/messages` (and its `count_tokens`) to the real API with the placeholder auth
header replaced by the real one, refuses every other request, and streams responses unbuffered. So a
prompt-injected worker can neither read the credential nor reach any other host
(invariants 10 and 13).

Stdlib only and free of SEAM imports: the CLI mounts this one file read-only into a
container and runs it as `python3 proxy.py`.
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import os
import sys
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
                 timeout: float = 600.0) -> type[BaseHTTPRequestHandler]:
    """Build the request handler. `allow`: client networks served (None = any)."""
    up = urlsplit(upstream)
    conn_cls = http.client.HTTPSConnection if up.scheme == "https" else http.client.HTTPConnection
    real_auth = kind.header_value(secret)

    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.0: the response body ends at connection close, so an upstream
        # chunked stream (SSE) can be relayed as-is without re-chunking.
        protocol_version = "HTTP/1.0"

        def _refuse(self, code: int, why: str) -> None:
            self.log_message("refused %s %s: %s", self.command, self.path, why)
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(
                b'{"type":"error","error":{"type":"permission_error",'
                b'"message":"refused by SEAM credential proxy"}}')

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

            headers = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP | _AUTH_HEADERS}
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
                while chunk := resp.read1(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError as e:
                self.log_message("upstream error: %s", type(e).__name__)
                if not self.wfile.closed:
                    try:
                        self.send_error(502)
                    except OSError:
                        pass
            finally:
                conn.close()

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = _handle

        def log_message(self, fmt: str, *args: object) -> None:
            # Request lines and refusals only; headers (which hold the credential) and
            # bodies are never logged.
            sys.stderr.write("seam-proxy: " + (fmt % args) + "\n")

    return Handler


def serve(kind: CredentialKind, secret: str, listen: tuple[str, int],
          upstream: str = DEFAULT_UPSTREAM, allow=None) -> ThreadingHTTPServer:
    """Create (not start) the proxy server; call `serve_forever()` on the result."""
    return ThreadingHTTPServer(listen, make_handler(kind, secret, upstream, allow))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--credential-env", required=True, choices=sorted(CREDENTIAL_KINDS))
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--upstream", default=DEFAULT_UPSTREAM)
    p.add_argument("--allow", action="append", default=None,
                   help="client CIDR to serve (repeatable; default: any)")
    args = p.parse_args(argv)
    secret = os.environ.get(args.credential_env)
    if not secret:
        print(f"seam-proxy: {args.credential_env} is not set", file=sys.stderr)
        return 2
    allow = [ipaddress.ip_network(c) for c in args.allow] if args.allow else None
    server = serve(CREDENTIAL_KINDS[args.credential_env], secret,
                   ("0.0.0.0", args.port), args.upstream, allow)
    print(f"seam-proxy: listening on :{args.port}", file=sys.stderr, flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
