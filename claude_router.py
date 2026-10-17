"""claude-router: route Claude Code requests to Anthropic or a local LiteLLM."""
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import struct
import sys
import time
from dataclasses import dataclass, replace
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from routing import DEFAULT_CONFIG, Features, is_routed, latest_human_text, route as decide_route  # noqa: F401

CLASSIFIER_OFF = replace(DEFAULT_CONFIG, rules=tuple(replace(r, enabled=r.kind != "classifier") for r in DEFAULT_CONFIG.rules))

def local_model(model: str, sonnet: str, haiku: str) -> str:
    return haiku if "haiku" in model else sonnet


@dataclass
class Config:
    listen_host: str
    listen_port: int
    anthropic_url: str
    litellm_url: str
    litellm_key: str
    local_sonnet: str
    local_haiku: str


def load_config(env: Mapping[str, str]) -> Config:
    key = env.get("LITELLM_API_KEY")
    if not key:
        raise ValueError("LITELLM_API_KEY is required")
    listen = env.get("CLAUDE_ROUTER_LISTEN", "127.0.0.1:8787")
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = int(port)
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("CLAUDE_ROUTER_LISTEN must be a loopback host:port, got %r" % listen)
    return Config(
        listen_host=host,
        listen_port=port,
        anthropic_url=env.get("CLAUDE_ROUTER_ANTHROPIC_URL", "https://api.anthropic.com"),
        litellm_url=env.get("LITELLM_URL", "http://127.0.0.1:4000"),
        litellm_key=key,
        local_sonnet=env.get("CLAUDE_ROUTER_LOCAL_SONNET", "claude-sonnet-4-6"),
        local_haiku=env.get("CLAUDE_ROUTER_LOCAL_HAIKU", "claude-haiku-4-5-20251001"),
    )


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"
UPSTREAM_TIMEOUT = 600
LOOPBACK_HOST_RE = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", re.I)
MIDSTREAM_SSE = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                 b'"message":"claude-router: upstream ended mid-stream"}}\n\n')


def local_headers(headers: Iterable[tuple[str, str]], key: str) -> list[tuple[str, str]]:
    """Client headers for LiteLLM: no client credentials, no oauth beta, our key."""
    out = []
    for k, v in headers:
        lk = k.lower()
        if lk in HOP or lk in ("authorization", "x-api-key"):
            continue
        if lk == "anthropic-beta":
            v = ",".join(b for b in (x.strip() for x in v.split(",")) if b and b != OAUTH_BETA)
            if not v:
                continue
        out.append((k, v))
    out.append(("Authorization", "Bearer " + key))
    return out


def make_server(cfg: Config) -> http.server.ThreadingHTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        # ponytail: HTTP/1.0 close-delimited responses, no keep-alive; switch to chunked if connection churn ever matters
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _error(self, status, msg):
            out = json.dumps({"type": "error", "error": {"type": "api_error", "message": msg}}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def handle_any(self):
            t0 = time.time()
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            # browsers (cross-origin) and DNS rebinding must not reach the injected LiteLLM key
            if "origin" in self.headers or not LOOPBACK_HOST_RE.fullmatch(self.headers.get("host", "")):
                self._error(403, "claude-router: forbidden origin/host")
                print("%s route=- reason=forbidden status=403" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      file=sys.stderr, flush=True)
                return
            f = Features.from_body(body) if is_routed(self.command, self.path) else None
            # Task 5 wires the live config + classifier; until then the classifier rule is off
            d = decide_route(CLASSIFIER_OFF, f, None) if f else None
            route, reason = (d.track, d.reason) if d else ("anthropic", "passthrough")
            model, stream = "-", False
            try:
                req = json.loads(body)
                model, stream = str(req.get("model", "-")), bool(req.get("stream"))
            except (ValueError, AttributeError):
                pass
            shown = model
            if route == "local":
                base, name = cfg.litellm_url, "LiteLLM"
                new = req["model"] = local_model(model, cfg.local_sonnet, cfg.local_haiku)
                body = json.dumps(req).encode()
                shown = "%s→%s" % (model, new)
                headers = local_headers(self.headers.items(), cfg.litellm_key)
            else:
                base, name = cfg.anthropic_url, "Anthropic"
                headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]
            u = urlsplit(base)
            Conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
            conn = Conn(u.hostname, u.port, timeout=UPSTREAM_TIMEOUT)
            status, err = 0, ""
            try:
                try:
                    conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                    conn.putheader("Host", u.netloc)
                    for k, v in headers:
                        conn.putheader(k, v)
                    if body or self.command in ("POST", "PUT", "PATCH"):
                        conn.putheader("Content-Length", str(len(body)))
                    conn.endheaders(body)
                    resp = conn.getresponse()
                except (OSError, http.client.HTTPException) as e:
                    status = 502
                    what = "timed out" if isinstance(e, TimeoutError) else "unreachable"
                    self._error(502, "claude-router: %s %s at %s" % (name, what, base))
                    return
                status = resp.status
                self.send_response(status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP or k.lower() == "content-length":
                        self.send_header(k, v)
                self.end_headers()
                while True:
                    try:
                        chunk = resp.read1(65536)
                    except (OSError, http.client.HTTPException):
                        chunk, err = b"", " err=midstream"
                    if not chunk:
                        # a short content-length body ends silently; resp.length is the bytes still owed
                        if resp.length:
                            err = " err=midstream"
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if err:  # headers are out, so tell the client the stream is truncated
                    if resp.getheader("content-type", "").startswith("text/event-stream"):
                        self.wfile.write(MIDSTREAM_SSE)
                    else:  # RST, so the client sees a failure rather than a clean EOF
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass  # client went away
            finally:
                conn.close()
                print("%s route=%s reason=%s model=%s status=%d ms=%d stream=%s%s" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), route, reason, shown, status,
                    (time.time() - t0) * 1000, stream, err), file=sys.stderr, flush=True)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any

    cls = http.server.ThreadingHTTPServer
    if ":" in cfg.listen_host:
        cls = type("V6Server", (cls,), {"address_family": socket.AF_INET6})
    srv = cls((cfg.listen_host, cfg.listen_port), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    try:
        cfg = load_config(os.environ)
    except ValueError as e:
        print("claude-router: %s" % e, file=sys.stderr)
        sys.exit(2)
    srv = make_server(cfg)
    print("listening on %s:%d; anthropic=%s litellm=%s" % (
        cfg.listen_host, cfg.listen_port, cfg.anthropic_url, cfg.litellm_url), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
