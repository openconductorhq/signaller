"""claude-router: route Claude Code requests to Anthropic or one of the downstreams set up in the console."""
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
from dataclasses import dataclass, replace
from typing import Iterable, Mapping
from urllib.parse import urlsplit

import console
import overrides  # after routing: overrides imports routing
from sessions import SessionDirectory, claude_dir
from console import LOOPBACK_HOST_RE
from classifier import Classifier
from doubling import AnswerCapture, Doubler
from prices import Prices, parse_valar
from routing import (session_of, strip_lead_tags, Downstream, Features, RoutingConfig, config_from_json, config_to_json, downstream, is_routed,
                     references, route as decide_route, track_model, track_window)
from keys import Keys
from store import NullStore, Store, load_console_config, save_console_config

@dataclass
class Config:
    listen_host: str
    listen_port: int
    anthropic_url: str
    env_valar: tuple[str, str] = ("", "")  # (VALAR_URL, VALAR_API_KEY): read once, by App.seed


def load_config(env: Mapping[str, str]) -> Config:
    listen = env.get("CLAUDE_ROUTER_LISTEN", "127.0.0.1:8787")
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = int(port)
    except ValueError:
        loopback = False
    # in a container the router must take connections from the bridge; the host publishes the port on 127.0.0.1 only
    if host == "0.0.0.0" and env.get("CLAUDE_ROUTER_IN_CONTAINER") == "1":
        loopback = True
    if not loopback:
        raise ValueError("CLAUDE_ROUTER_LISTEN must be a loopback host:port, got %r" % listen)
    return Config(
        listen_host=host,
        listen_port=port,
        anthropic_url=env.get("CLAUDE_ROUTER_ANTHROPIC_URL", "https://api.anthropic.com"),
        env_valar=(env.get("VALAR_URL", "").rstrip("/").removesuffix("/v1"), env.get("VALAR_API_KEY", "")),
    )


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"
WINDOW_HEADER = "X-Valar-Completion-Window"  # the router owns it: a client's own is dropped
UPSTREAM_TIMEOUT = 600
MIDSTREAM_SSE = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                 b'"message":"claude-router: upstream ended mid-stream"}}\n\n')


def upstream(app: "App", track: str, routing=None) -> tuple[str, str, str, Downstream | None]:
    """(display name, base url, key, downstream) of a track, from `routing` (the request's snapshot; default the live
    config) and the keys; anthropic has no key of ours.
    A track no longer configured comes back keyless, so it fails as "no key" rather than going anywhere else."""
    if track == "anthropic":
        return "Anthropic", app.cfg.anthropic_url, "", None
    ds = downstream(routing or app.routing, track)
    return (ds.name, ds.url, app.keys.get(track), ds) if ds else (track, "", "", None)


def upstream_headers(headers: Iterable[tuple[str, str]], key: str) -> list[tuple[str, str]]:
    """Client headers for a downstream: no client credentials, no oauth beta, our key."""
    out = []
    for k, v in headers:
        lk = k.lower()
        if lk in HOP or lk in ("authorization", "x-api-key", WINDOW_HEADER.lower()):
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
    named = lambda t: isinstance(t, dict) and isinstance(t.get("name"), str)
    rest = [t for t in old if not named(t)]  # kept as-is
    tools, changed = {t["name"]: t for t in old if named(t)}, False
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


def prepare(app: "App", track: str, model: str, req, headers, routing=None, f=None):
    """A request as it leaves for a track, the served one and its shadow alike:
    (name, base, key, ds, headers, out_model, window, changed, override). On a downstream the model is renamed, Valar's
    tool fix applied, the client's credentials swapped for the downstream's key, the window set and the matching
    settings lines applied. `req` (when a dict) is edited in place; `changed` says it must be re-serialised."""
    name, base, key, ds = upstream(app, track, routing)
    if not ds:
        return name, base, key, None, [(k, v) for k, v in headers if k.lower() not in HOP], model, None, False, None
    out_model = track_model(model, ds)
    hoisted = ds.kind == "valar" and isinstance(req, dict) and hoist_inline_tools(req)
    changed = isinstance(req, dict) and (hoisted or out_model != model)
    headers, window = upstream_headers(headers, key), track_window(model, ds)
    override = None
    if f is not None and (routing or app.routing).settings:
        try:
            applied = overrides.resolve((routing or app.routing).settings, ds.id, model, lambda n: f.get(n, lambda: "none"))
            override = overrides.describe(applied)
        except Exception as e:
            print("settings failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
        else:
            if "model" in applied:
                out_model, changed = applied["model"][0], True
            if "window" in applied:
                window = applied["window"][0]
    if changed:
        req["model"] = out_model
    if window:
        headers.append((WINDOW_HEADER, window))
    return name, base, key, ds, headers, out_model, window, changed, override


RETRY_DELAYS = (0.5, 1.5, 3)  # seconds before each retry of a connection failure (~5 s of blips covered)


def open_upstream(base: str, method: str, path: str, headers, body: bytes):
    """Like _open_upstream, retrying connection failures (DNS, refused, reset before a response).
    Nothing has reached the client yet, so a retry is invisible to it. Timeouts are not retried: one already
    waited UPSTREAM_TIMEOUT."""
    # uncompressed answers: the token capture can read any of them (Anthropic picks br/zstd when offered), and a
    # mid-stream error event appended to a gzip body would break the client's decoder (ZlibError)
    headers = [(k, v) for k, v in headers if k.lower() != "accept-encoding"]
    for delay in RETRY_DELAYS + (None,):
        try:
            return _open_upstream(base, method, path, headers, body)
        except (OSError, http.client.HTTPException) as e:
            if delay is None or isinstance(e, TimeoutError):
                raise
            time.sleep(delay)


def _open_upstream(base: str, method: str, path: str, headers, body: bytes):
    """Send the request as given to a base URL; returns (conn, resp). Raises OSError/HTTPException."""
    u = urlsplit(base)
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
        if self.routing.migrated_off:  # seed() below saves the migrated config, so this shows on the upgrade start only
            self.banner = " ".join(filter(None, [self.banner, "Switched off because they sent traffic to Local, which is gone: %s."
                                                 % ", ".join(self.routing.migrated_off)]))
        self._rlock = threading.Lock()
        self.session_levers = {}  # session id -> lever /router chose for that session only
        self.keys = Keys(config_dir)
        self.seed()
        self.classifier = Classifier(self._classifier_upstream)
        try:
            # CLAUDE_ROUTER_STATS_DIR: SQLite needs a real local disk, not a folder shared from the host (see compose.yaml)
            self.store = Store(os.path.join(os.path.expanduser(os.environ.get("CLAUDE_ROUTER_STATS_DIR") or config_dir), "stats.db"))
        except (OSError, sqlite3.Error) as e:  # broken stats must never cost Claude Code its router
            print("stats off: %s" % e, file=sys.stderr, flush=True)
            self.store = NullStore()
            self.banner = " ".join(filter(None, [self.banner, "Stats are off: %s." % e]))
        self.recent = collections.deque(maxlen=200)  # (Features, Decision, request_id)
        self.started = time.time()
        self.sessions = SessionDirectory(self.store, claude_dir(os.environ))
        self.prices = Prices(config_dir)
        self.doubler = Doubler(self.store, open_upstream, prices=self.prices)

    def _classifier_upstream(self, id: str) -> tuple[str, str, str] | None:
        _, url, key, ds = upstream(self, id)
        return (url, key, ds.kind) if ds else None

    def seed(self) -> None:
        """First start only: Valar's url and key from the environment. Later starts ignore it, so a deleted Valar stays deleted."""
        if "LITELLM_URL" in os.environ or "LITELLM_API_KEY" in os.environ:
            print("LITELLM_* is no longer used; add LiteLLM as a downstream in the console", file=sys.stderr, flush=True)
        if self.routing.seeded:
            return
        url, key = self.cfg.env_valar
        dss = tuple(replace(d, url=url) if d.id == "valar" and url else d for d in self.routing.downstreams)
        if key and downstream(self.routing, "valar"):
            self.keys.set("valar", key)
        self.set_routing(replace(self.routing, downstreams=dss, seeded=True))

    def refresh_prices(self) -> None:
        """Fetch each Valar-kind downstream's price list, keep it in memory and models.json, and price rows that were waiting. Never raises."""
        for d in self.routing.downstreams:
            _, url, key, _ = upstream(self, d.id)
            if d.kind != "valar" or not key:
                continue
            try:
                vp = parse_valar(console.fetch_models(url, key))
                if vp:  # a payload with no prices must not wipe good ones
                    self.prices.set_listed(d.id, vp)
                    console.save_models(self.config_dir, listed_prices=self.prices.listed)
            except Exception as e:  # reason only: never headers, never the key
                print("%s prices not refreshed: %s" % (d.id, getattr(e, "reason", None) or type(e).__name__),
                      file=sys.stderr, flush=True)
        try:
            self.store.reprice(self.prices, self.routing.downstreams)
        except Exception as e:
            print("reprice failed: %s" % type(e).__name__, file=sys.stderr, flush=True)

    def start_price_refresh(self, every: float = 3600) -> None:
        def loop():
            while True:
                self.refresh_prices()
                self.prune_settings()
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()

    def set_settings_for_session(self, sid: str, ds_id: str, tier: str | None, key: str, value: str | None,
                                 reset: bool = False) -> None:
        """Set (or with reset, clear) one session's settings lines, under the lock. ValueError if the line about to
        be written wouldn't load (upsert_session itself doesn't validate); nothing is saved then."""
        with self._rlock:
            if not reset:
                overrides.setting_from_json({"when": "session is %s" % sid.lower(), "downstream": ds_id, "tier": tier,
                                              "set": key, "value": value}, self.routing.downstreams, 1)
            st = overrides.reset_session(self.routing.settings, sid, ds_id) if reset else \
                overrides.upsert_session(self.routing.settings, sid, ds_id, tier, key, value)
            self._put(replace(self.routing, settings=st))

    def prune_settings(self) -> None:
        """Drop session ids not seen for a week from settings lines. Never raises; saves only when something went."""
        try:
            ids = {i for s in self.routing.settings if (lines := overrides.session_ids(s)) for i in lines}
            if isinstance(self.store, NullStore) or not ids:
                return
            last = self.store.session_last_seen(sorted(ids))
            new = overrides.prune(self.routing.settings, last.get, time.time())
            if new != self.routing.settings:
                self.set_routing(replace(self.routing, settings=new))
        except Exception as e:
            print("settings prune failed: %s" % type(e).__name__, file=sys.stderr, flush=True)

    def set_session_lever(self, sid: str, lever: str | None) -> None:
        # ponytail: memory only, a restart drops them (the status line shows it); one small entry per session that used /router
        if lever:
            self.session_levers[sid] = lever
        else:
            self.session_levers.pop(sid, None)

    def set_routing(self, new: RoutingConfig) -> None:
        with self._rlock:  # file and memory must agree under concurrent PUTs
            self._put(new)

    def _put(self, new: RoutingConfig) -> None:  # caller holds _rlock
        save_console_config(os.path.join(self.config_dir, "console.json"), new)
        self.routing = new  # single reference assignment; requests read it once

    def add_downstream(self, d: dict, key: str) -> Downstream:
        """Append a downstream (its config JSON) with no shadow of its own, and its key. ValueError if the config would be invalid."""
        with self._rlock:
            j = config_to_json(self.routing)
            j["downstreams"].append(d)
            j["doubling"]["shadow"][d["id"]] = None
            new = config_from_json(j)
            self._put(new)
            if key:
                self.keys.set(d["id"], key)
            return new.downstreams[-1]

    def delete_downstream(self, id: str) -> list[str]:
        """Remove a downstream, its own shadow and its key; if anything still points at it, remove nothing and return those."""
        with self._rlock:
            refs = references(self.routing, id, self.session_levers)
            if refs:
                return refs
            j = config_to_json(self.routing)
            j["downstreams"] = [d for d in j["downstreams"] if d["id"] != id]
            del j["doubling"]["shadow"][id]
            self._put(config_from_json(j))
            self.keys.delete(id)
            return []


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
            # browsers (cross-origin) and DNS rebinding must not reach the injected downstream keys; checked before
            # reading the body, which is then left unread, so the connection closes
            if "origin" in self.headers or not LOOPBACK_HOST_RE.fullmatch(self.headers.get("host", "")):
                self.close_connection = True
                self._error(403, "claude-router: forbidden origin/host")
                print("%s route=- reason=forbidden status=403 shadow=-" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      file=sys.stderr, flush=True)
                return
            try:
                n = int(self.headers.get("content-length") or 0)
                if n < 0:
                    raise ValueError
            except ValueError:
                self.close_connection = True
                return self._error(400, "claude-router: bad content-length")
            body = self.rfile.read(n)
            routing = app.routing  # read once; a concurrent set_routing doesn't affect this request
            model, stream, req = "-", False, None
            try:
                req = json.loads(body)
                model, stream = str(req.get("model", "-")), bool(req.get("stream"))
            except (ValueError, AttributeError):
                pass
            any_sid = session_of(self.headers, req)
            lever = app.session_levers.get(any_sid)
            if lever:  # /router set this session's own lever
                routing = replace(routing, lever=lever)
            try:  # a bad saved config must answer, not drop the connection; never guess a track
                f = Features.from_body(body, req) if is_routed(self.command, self.path) else None
                if f is not None:
                    f.session, f.hour = any_sid, time.localtime(t0).tm_hour
                d = decide_route(routing, f, app.classifier) if f else None
            except Exception as e:
                self._error(500, "claude-router: routing failed")
                print("%s route=- reason=error status=500 err=%s shadow=-" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), type(e).__name__), file=sys.stderr, flush=True)
                return
            route, reason = (d.track, d.reason) if d else ("anthropic", "passthrough")
            shown = model
            rec = f is not None and self.path.split("?", 1)[0] == "/v1/messages"
            sid = any_sid if rec else None
            rid = app.store.next_id() if rec else None
            if rec and sid:
                try:
                    app.sessions.note(sid, t0)
                except Exception:
                    pass
            pair, cap, shadow, turn_id = None, None, "-", None
            try:  # a doubling fault must never change the served request
                turn_id = app.doubler.turn_for(f, route, routing.doubling, sid) if rec and app.doubler else None
                if turn_id:  # shadow starts before the served upstream; it never touches the served path again
                    pair = app.doubler.pending_served(turn_id, rid, route, routing.doubling.shadow[route])
                    shadow = app.doubler.start_shadow(turn_id, rid, f, route, self.command, self.path,
                                                      self.headers.items(), body, app, routing)  # the shadow strips its own tags
            except Exception as e:
                print("doubling failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
                pair, shadow, turn_id = None, "-", None
            stripped = False
            try:  # routing already read the tags; no upstream needs them
                stripped = bool(f) and isinstance(req, dict) and strip_lead_tags(req)
            except Exception:
                pass
            name, base, key, ds, headers, out_model, window, changed, override = prepare(app, route, model, req, self.headers.items(), routing, f)
            if stripped or changed:
                body = json.dumps(req).encode()
            if out_model != model:
                shown = "%s→%s" % (model, out_model)
            status, err, ttfb, conn = 0, "", None, None
            try:
                if route != "anthropic" and not key:  # never fall back to another track
                    status = 502
                    self._error(502, "claude-router: %s has no key; set it in the console" % name)
                    return
                try:
                    conn, resp = open_upstream(base, self.command, self.path, headers, body)
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
                try:  # pricing never fails the request: unknown → nulls
                    cost, api_cost = app.prices.cost(ds, out_model, window, usage), app.prices.api_cost(model, usage)
                except Exception:
                    cost = api_cost = None
                if pair:
                    res = res or AnswerCapture.EMPTY
                    pair.finish({**res, "status": status, "ttfb_ms": ttfb, "ms": ms,
                                 "error": err.strip() or res.get("error")})
                if rec:
                    app.store.record_request({
                        "id": rid, "ts": t0, "track": route, "reason": d.reason, "explanation": json.dumps(d.explanation),
                        "model_in": model, "model_out": out_model, "status": status, "ttfb_ms": ttfb if ttfb is not None else ms,
                        "ms": ms, "stream": int(stream), "preview": ("↳ " + " · ".join(f.step.split("\n")) if f.step else f.prompt)[:120], "step": f.step, "prompt": f.prompt, "turn_id": turn_id,
                        "session_id": sid, "in_tok": usage.get("in"), "out_tok": usage.get("out"),
                        "cache_read_tok": usage.get("cache_read"), "cache_write_tok": usage.get("cache_write"),
                        "cache_write_1h_tok": usage.get("cache_write_1h"), "window": window, "override": override,
                        "cost": cost, "api_cost": api_cost})
                    f.prompt = None  # recorded; the preview deque keeps only the capped text
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
    app.start_price_refresh()  # its thread also prunes settings on its first pass
    print("listening on %s:%d; anthropic=%s downstreams=%s" % (
        cfg.listen_host, cfg.listen_port, cfg.anthropic_url,
        ", ".join("%s (%s)" % (d.id, "key set" if app.keys.has(d.id) else "no key") for d in app.routing.downstreams)),
        file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
