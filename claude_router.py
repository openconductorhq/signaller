"""claude-router: route Claude Code requests to Anthropic, Valar or a local LiteLLM."""
import collections
import http.client
import http.server
import ipaddress
import json
import os
import re
import socket
import sqlite3
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Iterable, Mapping
from urllib.parse import urlsplit

import console
from sessions import SessionDirectory, claude_dir
from console import LOOPBACK_HOST_RE
from classifier import Classifier
from doubling import AnswerCapture, Doubler
from routing import session_of, Features, RoutingConfig, is_routed, route as decide_route, track_model, track_window
from store import NullStore, Store, load_console_config, save_console_config

@dataclass
class Config:
    listen_host: str
    listen_port: int
    anthropic_url: str
    litellm_url: str
    litellm_key: str
    valar_url: str = "https://api.valarhq.ai"
    valar_key: str = ""


def load_config(env: Mapping[str, str]) -> Config:
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
        litellm_key=env.get("LITELLM_API_KEY", ""),
        valar_url=env.get("VALAR_URL", "https://api.valarhq.ai").rstrip("/").removesuffix("/v1"),
        valar_key=env.get("VALAR_API_KEY", ""),
    )


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"
UPSTREAM_TIMEOUT = 600
MIDSTREAM_SSE = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                 b'"message":"claude-router: upstream ended mid-stream"}}\n\n')


KEY_VAR = {"valar": "VALAR_API_KEY", "local": "LITELLM_API_KEY"}


def upstream(cfg: Config, track: str) -> tuple[str, str, str]:
    """(display name, base url, key) of a track; anthropic has no key of ours."""
    if track == "valar":
        return "Valar", cfg.valar_url, cfg.valar_key
    if track == "local":
        return "LiteLLM", cfg.litellm_url, cfg.litellm_key
    return "Anthropic", cfg.anthropic_url, ""


def upstream_headers(headers: Iterable[tuple[str, str]], key: str) -> list[tuple[str, str]]:
    """Client headers for Valar/LiteLLM: no client credentials, no oauth beta, our key."""
    out = []
    for k, v in headers:
        lk = k.lower()
        if lk in HOP or lk in ("authorization", "x-api-key", "x-valar-completion-window"):  # the router owns the window
            continue
        if lk == "anthropic-beta":
            v = ",".join(b for b in (x.strip() for x in v.split(",")) if b and b != OAUTH_BETA)
            if not v:
                continue
        out.append((k, v))
    out.append(("Authorization", "Bearer " + key))
    return out


def hoist_inline_tools(req: dict) -> bool:
    """Valar only: it 400s on Claude Code's inline-tools `tool_addition` content blocks ("type tool_addition,
    which the Messages API does not define"). Move each carried definition into top-level `tools` (a later
    same-name one replaces it), drop the blocks and any message they leave empty. True if anything changed."""
    old = req.get("tools") or []
    rest = [t for t in old if not (isinstance(t, dict) and isinstance(t.get("name"), str))]  # kept as-is
    tools, changed = {t["name"]: t for t in old if t not in rest}, False
    for m in req.get("messages") or []:
        c = m.get("content") if isinstance(m, dict) else None
        adds = [b for b in c if isinstance(b, dict) and b.get("type") == "tool_addition"] if isinstance(c, list) else []
        for b in adds:
            t = b.get("tool")
            d = t.get("definition") if isinstance(t, dict) and t.get("type") == "tool_definition" else None
            if isinstance(d, dict) and isinstance(d.get("name"), str):
                tools[d["name"]] = d
        if adds:
            changed, m["content"] = True, [b for b in c if b not in adds]
    if changed:
        req["messages"] = [m for m in req["messages"] if not (isinstance(m, dict) and m.get("content") == [])]
        req["tools"] = rest + list(tools.values())
    return changed


def open_upstream(cfg: Config, track: str, method: str, path: str, headers, body: bytes):
    """Send the request as given to the track's base URL; returns (conn, resp). Raises OSError/HTTPException."""
    u = urlsplit(upstream(cfg, track)[1])
    Conn = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conn = Conn(u.hostname, u.port, timeout=UPSTREAM_TIMEOUT)
    try:
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", u.netloc)
        for k, v in headers:
            conn.putheader(k, v)
        if body or method in ("POST", "PUT", "PATCH"):
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        return conn, conn.getresponse()
    except BaseException:
        conn.close()
        raise


class App:
    def __init__(self, cfg: Config, config_dir: str):
        os.makedirs(config_dir, mode=0o700, exist_ok=True)
        self.cfg, self.config_dir = cfg, config_dir
        self.routing, self.banner = load_console_config(os.path.join(config_dir, "console.json"))
        self.classifier = Classifier({"valar": (cfg.valar_url, cfg.valar_key), "local": (cfg.litellm_url, cfg.litellm_key)})
        try:
            self.store = Store(os.path.join(config_dir, "stats.db"))
        except (OSError, sqlite3.Error) as e:  # broken stats must never cost Claude Code its router
            print("stats off: %s" % e, file=sys.stderr, flush=True)
            self.store = NullStore()
            self.banner = " ".join(filter(None, [self.banner, "Stats are off: %s." % e]))
        self.recent = collections.deque(maxlen=200)  # (Features, Decision, request_id)
        self.started = time.time()
        self.sessions = SessionDirectory(self.store, claude_dir(os.environ))
        self.doubler = Doubler(self.store, open_upstream)
        self._rlock = threading.Lock()

    def set_routing(self, new: RoutingConfig) -> None:
        with self._rlock:  # file and memory must agree under concurrent PUTs
            save_console_config(os.path.join(self.config_dir, "console.json"), new)
            self.routing = new  # single reference assignment; requests read it once


def default_config_dir() -> str:
    return os.path.expanduser(os.environ.get("CLAUDE_ROUTER_CONFIG_DIR") or "~/.config/claude-router")


def make_server(cfg: Config, app: App | None = None) -> http.server.ThreadingHTTPServer:
    app = app or App(cfg, default_config_dir())

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
            p = urlsplit(self.path).path
            if p == "/ui" or p.startswith("/ui/"):
                return console.handle_ui(self, app)
            if p == "/favicon.ico":  # browsers ask for it on their own; never let it reach Anthropic
                return self._error(404, "not found")
            t0 = time.time()
            body = self.rfile.read(int(self.headers.get("content-length") or 0))
            # browsers (cross-origin) and DNS rebinding must not reach the injected Valar/LiteLLM keys
            if "origin" in self.headers or not LOOPBACK_HOST_RE.fullmatch(self.headers.get("host", "")):
                self._error(403, "claude-router: forbidden origin/host")
                print("%s route=- reason=forbidden status=403 shadow=-" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      file=sys.stderr, flush=True)
                return
            routing = app.routing  # read once; a concurrent set_routing doesn't affect this request
            try:  # a bad saved config must answer, not drop the connection; never guess a track
                f = Features.from_body(body) if is_routed(self.command, self.path) else None
                d = decide_route(routing, f, app.classifier) if f else None
            except Exception as e:
                self._error(500, "claude-router: routing failed")
                print("%s route=- reason=error status=500 err=%s shadow=-" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__), file=sys.stderr, flush=True)
                return
            route, reason = (d.track, d.reason) if d else ("anthropic", "passthrough")
            model, stream, req = "-", False, None
            try:
                req = json.loads(body)
                model, stream = str(req.get("model", "-")), bool(req.get("stream"))
            except (ValueError, AttributeError):
                pass
            shown = out_model = model
            rec = f is not None and self.path.split("?", 1)[0] == "/v1/messages"
            sid = session_of(self.headers, req) if rec else None
            rid = app.store.next_id() if rec else None
            if rec and sid:
                try:
                    app.sessions.note(sid, t0)
                except Exception:
                    pass
            window = None
            pair, cap, shadow, turn_id = None, None, "-", None
            try:  # a doubling fault must never change the served request
                turn_id = app.doubler.turn_for(f, route, routing.doubling, sid) if rec and app.doubler else None
                if turn_id:  # shadow starts before the served upstream; it never touches the served path again
                    pair = app.doubler.pending_served(turn_id, rid, None, route, routing.doubling.shadow[route])
                    shadow = app.doubler.start_shadow(turn_id, rid, f, route, self.command, self.path,
                                                      self.headers.items(), body, cfg, routing.models, windows=routing.windows)
            except Exception as e:
                print("doubling failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
                pair, shadow, turn_id = None, "-", None
            name, base, key = upstream(cfg, route)
            if route != "anthropic":
                out_model = track_model(model, routing.models[route])
                hoisted = route == "valar" and isinstance(req, dict) and hoist_inline_tools(req)
                if out_model != model or hoisted:
                    req["model"] = out_model
                    body = json.dumps(req).encode()
                if out_model != model:
                    shown = "%s→%s" % (model, out_model)
                headers = upstream_headers(self.headers.items(), key)
                if route == "valar":
                    window = track_window(model, routing.windows["valar"])
                    headers.append(("X-Valar-Completion-Window", window))
            else:
                headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP]
            status, err, ttfb, conn = 0, "", None, None
            try:
                if route != "anthropic" and not key:  # never fall back to another track
                    status = 502
                    self._error(502, "claude-router: %s not configured (%s unset)" % (name, KEY_VAR[route]))
                    return
                try:
                    conn, resp = open_upstream(cfg, route, self.command, self.path, headers, body)
                    ttfb = int((time.time() - t0) * 1000)
                except (OSError, http.client.HTTPException) as e:
                    status = 502
                    what = "timed out" if isinstance(e, TimeoutError) else "unreachable"
                    self._error(502, "claude-router: %s %s at %s" % (
                        name, what, base))
                    return
                status = resp.status
                self.send_response(status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP or k.lower() == "content-length":
                        self.send_header(k, v)
                self.end_headers()
                if rec:
                    cap = AnswerCapture(resp.getheader("content-type", ""), resp.getheader("content-encoding", ""))
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
                    if cap:  # only after the bytes are out
                        cap.feed(chunk)
                if err:  # headers are out, so tell the client the stream is truncated
                    if resp.getheader("content-type", "").startswith("text/event-stream"):
                        self.wfile.write(MIDSTREAM_SSE)
                    else:  # RST, so the client sees a failure rather than a clean EOF
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            except OSError:
                pass  # client went away
            finally:
                if conn:
                    conn.close()
                ms = int((time.time() - t0) * 1000)
                try:  # result() consumes a JSON body: call it once
                    res = cap.result() if cap else {}
                except Exception:
                    res = {}
                usage = res.get("usage") or {}
                if pair:
                    res = res or {"text": "", "tools": [], "output_tokens": None}
                    pair.finish({**res, "status": status, "ttfb_ms": ttfb, "ms": ms,
                                 "error": err.strip() or res.get("error")})
                if rec:
                    app.store.record_request({
                        "id": rid, "ts": t0, "track": route, "reason": d.reason, "explanation": json.dumps(d.explanation),
                        "model_in": model, "model_out": out_model, "status": status, "ttfb_ms": ttfb if ttfb is not None else ms,
                        "ms": ms, "stream": int(stream), "preview": f.text[:120], "turn_id": turn_id,
                        "session_id": sid, "in_tok": usage.get("in"), "out_tok": usage.get("out"),
                        "cache_read_tok": usage.get("cache_read"), "cache_write_tok": usage.get("cache_write"), "window": window})
                    app.recent.append((f, d, rid))
                shown_reason = '"%s"' % reason if re.search(r"\s", reason) else reason
                print("%s route=%s reason=%s model=%s status=%d ms=%d stream=%s%s shadow=%s" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), route, shown_reason, shown, status, ms, stream, err, shadow),
                    file=sys.stderr, flush=True)

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
    try:
        app = App(cfg, default_config_dir())
    except OSError as e:
        print("claude-router: config dir unusable: %s" % e, file=sys.stderr)
        sys.exit(2)
    srv = make_server(cfg, app)
    print("listening on %s:%d; anthropic=%s valar=%s (%s) litellm=%s (%s)" % (
        cfg.listen_host, cfg.listen_port, cfg.anthropic_url, cfg.valar_url, "key set" if cfg.valar_key else "no key",
        cfg.litellm_url, "key set" if cfg.litellm_key else "no key"), file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
