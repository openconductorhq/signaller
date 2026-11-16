"""signaller: route Claude Code requests to Anthropic or one of the downstreams set up in the console."""
import collections
import http.client
import http.server
import ipaddress
import json
import os
import re
import select
import socket
import sqlite3
import struct
import sys
import threading
import time
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, NamedTuple
from urllib.parse import urlsplit

import autophase
import autotrain
import console
import overrides  # after routing: overrides imports routing
import presets
from autophase import BoundedDict
from sessions import SessionDirectory, claude_dir
from console import LOOPBACK_HOST_RE
from classifier import Classifier
from doubling import AnswerCapture, Doubler, sum_usage
from keys import host_of
from prices import Prices, parse_valar
from routing import (DEFAULT_CONFIG, LEVER_MODES, has_images, session_of, strip_lead_tags, text_hash, Decision, Downstream, Features, RoutingConfig,
                     config_from_json, config_to_json, downstream, is_routed, references, route as decide_route,
                     track_model, track_window, tracks, unwire)
import advisor
from keys import Keys, write_json
from store import NullStore, Store, load_console_config, save_console_config

@dataclass
class Config:
    listen_host: str
    listen_port: int
    anthropic_url: str
    env_valar: tuple[str, str] = ("", "")  # (VALAR_URL, VALAR_API_KEY): read once, by App.seed


def load_config(env: Mapping[str, str]) -> Config:
    listen = env.get("SIGNALLER_LISTEN", "127.0.0.1:8787")
    host, _, port = listen.rpartition(":")
    host = host.strip("[]")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
        port = int(port)
    except ValueError:
        loopback = False
    # in a container the router must take connections from the bridge; the host publishes the port on 127.0.0.1 only
    if host == "0.0.0.0" and env.get("SIGNALLER_IN_CONTAINER") == "1":
        loopback = True
    if not loopback:
        raise ValueError("SIGNALLER_LISTEN must be a loopback host:port, got %r" % listen)
    return Config(
        listen_host=host,
        listen_port=port,
        anthropic_url=env.get("SIGNALLER_ANTHROPIC_URL", "https://api.anthropic.com"),
        env_valar=(env.get("VALAR_URL", "").rstrip("/").removesuffix("/v1"), env.get("VALAR_API_KEY", "")),
    )


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailer", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
OAUTH_BETA = "oauth-2025-04-20"
WINDOW_HEADER = "X-Valar-Completion-Window"  # the router owns it: a client's own is dropped
UPSTREAM_TIMEOUT = 600
MIDSTREAM_SSE = (b'event: error\ndata: {"type":"error","error":{"type":"api_error",'
                 b'"message":"signaller: upstream ended mid-stream"}}\n\n')
ADVICE_CAP = 4 << 20  # bytes read of an advice answer; a longer one fails as unparseable
PING_EVERY = 10.0  # seconds between SSE pings to Claude Code while an advice call runs (no bytes flow otherwise)
PING_SSE = advisor.sse({"type": "ping"})
ISSUED_BYTES = advisor.ISSUED.encode()


def upstream(app: "App", track: str, routing=None) -> tuple[str, str, str, Downstream | None]:
    """(display name, base url, key, downstream) of a track, from `routing` (the request's snapshot; default the live
    config) and the keys; anthropic has no key of ours.
    A track no longer configured comes back keyless, so it fails as "no key" rather than going anywhere else."""
    if track == "anthropic":
        return "Anthropic", app.cfg.anthropic_url, "", None
    ds = downstream(routing or app.routing, track)
    return (ds.name, ds.url, app.keys.get(track, ds.url), ds) if ds else (track, "", "", None)


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


class AdvisorPlan(NamedTuple):
    """How a downstream runs a turn's advisor calls."""
    model: str   # the model the calls run on
    window: str | None
    asked: str   # the advisor model Claude Code named (the row's model_in, priced on Anthropic for the savings)


class Prepared(NamedTuple):
    """A request as prepare() sends it on."""
    name: str
    base: str
    key: str
    ds: Downstream | None
    headers: list
    model: str          # the model it goes out as
    window: str | None
    changed: bool       # the body must be re-serialised
    override: str | None
    plan: AdvisorPlan | None


def settings_lines(settings, ds: Downstream, model: str, f) -> tuple[dict, str | None]:
    """(the settings lines for a model on this downstream, as overrides.resolve gives them; the row's override
    column). ({}, None) without lines or features, or on a failure, which is logged and never fails the request."""
    if f is None or not settings:
        return {}, None
    try:
        applied = overrides.resolve(settings, ds.id, model, lambda n: f.get(n, lambda: "none"))
        return applied, overrides.describe(applied)
    except Exception as e:
        print("settings failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
        return {}, None


def fit_window(app: "App", ds: Downstream, window, model):
    """A model without the window the router is about to send falls back to Now, the same rule the Tracks row
    applies; a model Valar hasn't priced (no listed windows) goes out as set."""
    if ds.kind == "valar" and window and (listed := app.prices.listed.get(ds.id, {}).get(model)) and window not in listed:
        return "asap"
    return window


def prepare(app: "App", track: str, model: str, req, headers, routing=None, f=None, advise=False,
            raw: bytes | None = None) -> Prepared:
    """A request as it leaves for a track, the served one and its shadow alike. On a downstream the model is renamed,
    Valar's tool fix applied, the client's credentials swapped for the downstream's key, the window set and the
    matching settings lines applied. The Advisor is stripped, or with `advise` (a streaming /v1/messages request) on a
    downstream that has it on, offered as a client tool, `plan` saying which model and window run its calls.
    `req` (when a dict) is edited in place; `changed` says it must be re-serialised. `raw`, the body `req` was parsed
    from, lets the advisor scans be skipped when its bytes can't hold what they look for (None: always scan)."""
    name, base, key, ds = upstream(app, track, routing)
    if not ds:
        # any lever: a downstream's unsigned thinking can't reach Anthropic (it 400s on it) — in auto after a
        # downstream turn, in rules after a switch back, or mid-turn on the image fallback. Nothing of theirs to
        # drop → the request goes out byte for byte. The advisor pairs the router issued go too: Anthropic never did.
        dropped = False
        if track == "anthropic" and isinstance(req, dict):
            dropped = autophase.drop_old_thinking(req)
            # the issued ids are ASCII, which no JSON encoder escapes: absent from the bytes, absent from req
            if raw is None or ISSUED_BYTES in raw:
                dropped = advisor.drop_issued(req) or dropped
        return Prepared(name, base, key, None, [(k, v) for k, v in headers if k.lower() not in HOP], model, None, dropped,
                        None, None)
    out_model = track_model(model, ds)
    hoisted = ds.kind == "valar" and isinstance(req, dict) and hoist_inline_tools(req)
    # every advisor definition, call and result names "advisor" (a type advisor_*, a name advisor): without those
    # bytes there's nothing to offer or strip
    scan = isinstance(req, dict) and (raw is None or b"advisor" in raw)
    adef = advisor.definition(req) if scan and advise and ds.advisor else None
    adv_model = adef.get("model") if adef else None
    if isinstance(adv_model, str) and adv_model:
        stripped = advisor.to_client_form(req)
    else:
        adv_model, stripped = None, scan and advisor.strip_advisor(req)
    changed = isinstance(req, dict) and (hoisted or stripped or out_model != model)
    headers, window = upstream_headers(headers, key), track_window(model, ds)
    settings = (routing or app.routing).settings
    applied, override = settings_lines(settings, ds, model, f)
    if "model" in applied:
        out_model, changed = applied["model"][0], True
    if "window" in applied:
        window = applied["window"][0]
    window = fit_window(app, ds, window, out_model)
    plan = None
    if adv_model:
        m = track_model(adv_model, ds)
        m = settings_lines(settings, ds, adv_model, f)[0].get("model", (m,))[0]
        plan = AdvisorPlan(m, fit_window(app, ds, window, m), adv_model)
    if changed:
        req["model"] = out_model
    if window:
        headers.append((WINDOW_HEADER, window))
    return Prepared(name, base, key, ds, headers, out_model, window, changed, override, plan)


def request_row(app: "App", ds, usage: dict, model_in, model_out, window, status, ttfb, ms, **cols) -> dict:
    """A `requests` row: cols, plus the models, window, status and timings, the usage's token columns and its price
    (cost on ds; api_cost, what model_in would have cost on Anthropic). Pricing never fails the row: unknown → nulls."""
    try:
        cost, api_cost = app.prices.cost(ds, model_out, window, usage), app.prices.api_cost(model_in, usage)
    except Exception:
        cost = api_cost = None
    return {**cols, "model_in": model_in, "model_out": model_out, "window": window, "status": status,
            "ttfb_ms": ttfb if ttfb is not None else ms, "ms": ms, "in_tok": usage.get("in"), "out_tok": usage.get("out"),
            "cache_read_tok": usage.get("cache_read"), "cache_write_tok": usage.get("cache_write"),
            "cache_write_1h_tok": usage.get("cache_write_1h"), "cost": cost, "api_cost": api_cost}


def run_pinging(fn, write) -> tuple:
    """Run fn() on a worker thread, writing PING_SSE through write every PING_EVERY seconds until it returns: pings
    keep a client's idle watchdog off a stream that has nothing else to send. (fn's result, or None if it raised;
    gone: a ping write failed, the client went away). The wait goes on after that: fn still finishes, so a row it
    records is recorded."""
    got, gone = [], False
    worker = threading.Thread(target=lambda: got.append(fn()), daemon=True)
    worker.start()
    while True:
        worker.join(PING_EVERY)
        if not worker.is_alive():
            return (got[0] if got else None), gone
        if not gone:
            try:
                write(PING_SSE)
            except OSError:
                gone = True


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
        self.lever_path = os.path.join(config_dir, "session_levers.json")
        self.session_levers = self._load_levers()  # session id -> lever /signaller chose for that session only
        self.first_party = None  # the latest plugin report: Claude Code runs with the first-party flag (None: no report yet)
        self.notices = {}  # session id -> {"turn", "text"}: the plugin shows it once (an image fallback)
        self.rev = 0  # config version, bumped by every _put: a stale whole-config PUT is refused with it
        self._turn_hours = BoundedDict(512)  # turn_key -> the hour its turn runs at
        # ponytail: 512 live turns, oldest dropped; a turn outliving them re-pins at a later hour boundary
        self._collisions = (None, [])  # (settings tuple, its collisions), memoised for the 1-second console poll
        self.keys = Keys(config_dir)
        self.migrate_keys()
        self.seed()
        self.presets, pbanner = presets.load(config_dir)
        if pbanner:
            self.banner = " ".join(filter(None, [self.banner, pbanner]))
        self._finish_switch()
        # ponytail: 1024 turns / 1024 sessions, oldest dropped; an evicted turn is decided again from its history
        self._auto_lock, self._auto, self.auto_last = threading.Lock(), BoundedDict(1024), BoundedDict(1024)
        self.auto_model = self._load_auto_model()
        self.classifier = Classifier(self._classifier_upstream)
        try:
            # SIGNALLER_STATS_DIR: SQLite needs a real local disk, not a folder shared from the host (see compose.yaml)
            self.store = Store(os.path.join(os.path.expanduser(os.environ.get("SIGNALLER_STATS_DIR") or config_dir), "stats.db"))
        except (OSError, sqlite3.Error) as e:  # broken stats must never cost Claude Code its router
            print("stats off: %s" % e, file=sys.stderr, flush=True)
            self.store = NullStore()
            self.banner = " ".join(filter(None, [self.banner, "Stats are off: %s." % e]))
        self.recent = collections.deque(maxlen=200)  # (Features, Decision, request_id)
        self.started = time.time()
        self.sessions = SessionDirectory(self.store, claude_dir(os.environ))
        self.trainer = autotrain.Trainer(config_dir, self.sessions.dir, lambda: self.routing.auto)
        self.prices = Prices(config_dir)
        self.doubler = Doubler(self.store, open_upstream, prices=self.prices)

    def migrate_keys(self) -> None:
        """Tie the keys still without a host (an old flat file's, or ones a start with an unreadable console.json
        left hanging) to the current setup. Never raises: a failed write is bannered and retried at the next start.
        A default-fallback config is never migrated against — its hosts aren't the person's."""
        try:
            if self.routing is not DEFAULT_CONFIG:
                self.keys.migrate({d.id: d.url for d in self.routing.downstreams})
        except OSError as e:
            print("key migration not written: %s" % type(e).__name__, file=sys.stderr, flush=True)
            self.banner = " ".join(filter(None, [self.banner,
                                                "Your downstream keys couldn't be written; they're retried at the next start."]))

    def _finish_switch(self) -> None:
        """The active preset still carrying its config means activate_preset was interrupted between its two
        writes: finish it — put that config into console.json, then clear it. Never raises: a failure is
        bannered and the store stays as found, so the next start tries again."""
        entry = next((p for p in self.presets["presets"] if p["name"] == self.presets["active"]), None)
        if not entry or entry["config"] is None:
            return
        try:
            with self._rlock:  # no server threads yet; the lock for the same invariant as every other write
                self._put(replace(config_from_json(entry["config"]), seeded=True, migrated_off=()))
                entry["config"] = None
                self._save_presets()
        except Exception as e:
            print("preset switch not finished: %s" % type(e).__name__, file=sys.stderr, flush=True)
            self.banner = " ".join(filter(None, [self.banner,
                                                 "A preset switch was interrupted and couldn't be finished here; switch presets once more from the console."]))

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
        vd = next((d for d in dss if d.id == "valar"), None)
        if key and vd:
            self.keys.set("valar", vd.url, key)
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
                self.prune_levers()
                time.sleep(every)
        threading.Thread(target=loop, daemon=True).start()

    def set_settings_for_session(self, sid: str, ds_id: str, tier: str | None, key: str, value: str | None,
                                 reset: bool = False) -> None:
        """Set (or with reset, clear) one session's settings lines, under the lock. ValueError if the line about to
        be written wouldn't load (the Setting is built and validated first); nothing is saved then."""
        with self._rlock:
            st = None if reset else overrides.setting_from_json(
                {"when": "session is %s" % sid.lower(), "downstream": ds_id, "tier": tier, "set": key, "value": value},
                self.routing.downstreams, 1)
            new = overrides.reset_session(self.routing.settings, sid, ds_id) if reset else \
                overrides.upsert_session(self.routing.settings, st)
            self._put(replace(self.routing, settings=new))

    def prune_settings(self) -> None:
        """Drop session ids not seen for a week (no sessions row counts as gone too) from settings lines.
        Never raises; saves only when something went."""
        try:
            with self._rlock:  # read, prune and put in one go: no read-modify-write race with set_settings_for_session
                ids = {i for s in self.routing.settings if (lines := overrides.session_ids(s)) for i in lines}
                if isinstance(self.store, NullStore) or not ids:
                    return
                last = self.store.session_last_seen(sorted(ids))
                new = overrides.prune(self.routing.settings, last.get, time.time())
                if new != self.routing.settings:
                    self._put(replace(self.routing, settings=new))
        except Exception as e:
            print("settings prune failed: %s" % type(e).__name__, file=sys.stderr, flush=True)

    def collisions(self):
        """Settings-line collisions, memoised per settings tuple: the console's 1-second poll would recompute."""
        key, val = self._collisions
        if key is not self.routing.settings:
            key, val = self.routing.settings, overrides.collisions(self.routing.settings)
            self._collisions = (key, val)
        return val

    def hour_of(self, turn_key, hour):
        """The hour a turn runs at: pinned at its first request, so a tool loop crossing an hour boundary doesn't
        switch model mid-turn."""
        return self._turn_hours.setdefault(turn_key, hour)

    def _load_auto_model(self):
        """The optional content model from the config dir. None when absent; unreadable → None plus a banner."""
        try:
            return autophase.Model.load(os.path.join(self.config_dir, "auto_model.json"))
        except FileNotFoundError:
            return None
        except Exception as e:  # the class name only, never the file's text
            self._bad_auto_banner = "Auto mode's model couldn't be read (%s); auto runs on the phase alone." % type(e).__name__
            self.banner = " ".join(filter(None, [self.banner, self._bad_auto_banner]))
            return None

    def auto_decide(self, f: Features, s: "autophase.AutoSettings") -> tuple[str, str]:
        """(phase, why) for auto, decided once per human turn: keyed on the message that opened it (turn_index), so
        a tool loop's steps agree even when Claude Code appends text to a tool result. The phase is read from the
        history up to and including that message — exactly what the turn's first request saw — so the cache is only
        a speed-up and the current turn's own tool calls can't flip it mid-turn. Never raises. The handler records
        the main-thread answer (Decision.auto) after route()."""
        try:
            i = autophase.turn_index(f.messages)
            text = autophase.human_text(f.messages[i]) if i >= 0 else ""
            key = (f.session, text_hash(text), i) if f.session else None
        except Exception:
            return "none", "phase error"
        with self._auto_lock:
            got = self._auto.get(key) if key else None
        if got is None:
            m, failed = self.auto_model, []

            def score(text):
                try:
                    return m.score(text)
                except Exception:
                    failed.append(True)
                    raise
            try:
                got = autophase.phase(f.messages if i < 0 else f.messages[:i + 1], s, score if m else None)
            except Exception:
                got = ("none", "model error") if failed else ("none", "phase error")
            if key:  # no session: nothing to share a turn with
                with self._auto_lock:
                    self._auto[key] = got
        return got

    def note_auto(self, f: Features, phase: str, why: str) -> None:
        """What auto last chose for a session's main thread: a subagent or teammate request doesn't overwrite it,
        and neither does a main-thread side request (a title generation: one message, no tools) — main-loop
        requests carry their tools, so only those count."""
        if f.session and f.tools and not f.subagent and not f.teammate:
            with self._auto_lock:
                self.auto_last[f.session] = {"phase": phase, "why": why}
                self.auto_last.move_to_end(f.session)

    def auto_for(self, sid: str) -> dict | None:
        with self._auto_lock:
            got = self.auto_last.get(sid)
            return dict(got) if got else None

    def use_trained_model(self) -> None:
        """Install the job's fitted model without a restart: auto_model.json written atomically,
        the live scorer swapped, the training folder (labels included) deleted, and the banner about an
        unreadable model dropped (this one replaces it). ValueError when nothing finished."""
        model = self.trainer.use()
        write_json(os.path.join(self.config_dir, "auto_model.json"), model)
        self.auto_model = autophase.Model(model["bias"], model["weights"])
        bad = getattr(self, "_bad_auto_banner", "")
        if bad and bad in self.banner:
            self.banner = " ".join(self.banner.replace(bad, "").split())
            self._bad_auto_banner = ""
        self.trainer.reset()

    def set_session_lever(self, sid: str, lever: str | None) -> None:
        """One small entry per session that used /signaller; saved so a restart keeps it."""
        if self.session_levers.get(sid) == lever:  # unchanged: skip the fsync (a re-assert, or a reset of a session with none)
            return
        with self._rlock:
            if lever:
                self.session_levers[sid] = lever
            else:
                self.session_levers.pop(sid, None)
            write_json(self.lever_path, self.session_levers)

    def _load_levers(self) -> dict[str, str]:
        try:
            with open(self.lever_path) as f:
                levers = json.load(f)
            if not (isinstance(levers, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in levers.items())):
                raise ValueError("not a {sid: lever} object")
            return levers
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:  # reason only, never the file's text; no banner: the levers are a convenience
            print("session levers unreadable: %s" % type(e).__name__, file=sys.stderr, flush=True)
            return {}

    def lever_for(self, sid: str | None) -> str | None:
        """The session's own /signaller lever, but only one the active setup can still follow (a restart, or a preset
        switch, may have dropped the track it named); otherwise the global lever applies. The raw entry is kept."""
        lever = self.session_levers.get(sid or "")
        if not lever:  # the common case: no entry, no tracks() scan
            return None
        return lever if lever in LEVER_MODES or lever in tracks(self.routing) else None

    def prune_levers(self) -> None:
        """Drop session ids not seen for a week (no sessions row counts as gone too; same rule as overrides.prune).
        Never raises; saves only when something went."""
        try:
            if isinstance(self.store, NullStore) or not self.session_levers:
                return
            last = self.store.session_last_seen(list(self.session_levers))  # the DB query stays outside the lock
            now = time.time()
            dead = [sid for sid in self.session_levers if overrides.gone(last.get(sid.lower()), now)]
            with self._rlock:  # re-check: a /signaller since the query may have revived one
                dead = [sid for sid in dead if sid in self.session_levers]
                if dead:
                    for sid in dead:
                        self.session_levers.pop(sid, None)
                    write_json(self.lever_path, self.session_levers)
        except Exception as e:
            print("lever prune failed: %s" % type(e).__name__, file=sys.stderr, flush=True)

    def set_routing(self, new: RoutingConfig) -> None:
        with self._rlock:  # file and memory must agree under concurrent PUTs
            self._put(new)

    def _put(self, new: RoutingConfig) -> None:  # caller holds _rlock
        save_console_config(os.path.join(self.config_dir, "console.json"), new)
        self.rev += 1  # a page holding an older whole-config copy must not overwrite this
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
                self.keys.set(d["id"], d["url"], key)
            self.migrate_keys()  # a key left without a host (an old flat file's) ties to the downstream just added
            return new.downstreams[-1]

    def delete_downstream(self, id: str, repoint: bool = False) -> list[str]:
        """Remove a downstream, its own shadow and its key; if anything still points at it, remove nothing and return those.
        With `repoint`, those are first pointed elsewhere (`unwire`) and its session levers dropped, in the same write.
        The key survives when an inactive preset still has the same id at the same host."""
        with self._rlock:
            refs = references(self.routing, id, self.session_levers)
            if refs and not repoint:
                return refs
            j = unwire(config_to_json(self.routing), id) if refs else config_to_json(self.routing)
            if any(lv == id for lv in self.session_levers.values()):
                self.session_levers = {s: lv for s, lv in self.session_levers.items() if lv != id}
                write_json(self.lever_path, self.session_levers)
            j["downstreams"] = [d for d in j["downstreams"] if d["id"] != id]
            del j["doubling"]["shadow"][id]
            ds = downstream(self.routing, id)  # before _put: the key is tied to the url it was set for
            self._put(config_from_json(j))
            shared = any(isinstance(d, dict) and d.get("id") == id and host_of(d.get("url", "")) == host_of(ds.url)
                         for p in self.presets["presets"]
                         if isinstance(p.get("config"), dict) and p["name"] != self.presets["active"]
                         for d in p["config"].get("downstreams", []))
            if ds and not shared:
                self.keys.delete(id, ds.url)
            return []

    def preset_names(self) -> list[str]:
        return [p["name"] for p in self.presets["presets"]]

    def _preset(self, name: str) -> dict:  # caller holds _rlock
        entry = next((p for p in self.presets["presets"] if p["name"] == name), None)
        if entry is None:
            raise KeyError(name)
        return entry

    def _save_presets(self) -> None:  # caller holds _rlock
        presets.save(self.config_dir, self.presets)

    def save_preset(self, name: str, tagline: str) -> None:
        """Save the current setup as a new inactive preset: session-bound lines and machine state dropped."""
        self.add_preset(name, tagline, config_to_json(self.routing))

    def add_preset(self, name: str, tagline: str, config: dict) -> None:
        """Add an inactive preset from a config JSON (the current setup, or an import): session-bound lines and
        machine state dropped. ValueError, nothing stored, when the config wouldn't load."""
        with self._rlock:
            new = presets.validated(config)  # ValueError propagates; nothing is stored then
            name, tagline = presets.check_name(name), presets.check_tagline(tagline)
            if name in self.preset_names():
                raise ValueError(f"a preset named {name} exists")
            self.presets["presets"].append({"name": name, "tagline": tagline, "config": config_to_json(new)})
            self._save_presets()

    def activate_preset(self, name: str) -> None:
        """Switch setups, crash-safe at every step: (a) presets.json with the previous preset's config set to a
        snapshot of the live config, active moved to the target — which still carries its own config; (b) the
        target's config put into console.json (rev bumps); (c) presets.json with the target's config cleared.
        A crash anywhere leaves a state the loader finishes; a failed put restores the store as it was.
        KeyError on an unknown name."""
        with self._rlock:
            if name == self.presets["active"]:
                return
            target = self._preset(name)
            new = replace(config_from_json(target["config"]), seeded=True, migrated_off=())
            prev, prev_name = self._preset(self.presets["active"]), self.presets["active"]
            try:
                prev["config"] = config_to_json(self.routing)  # the person's own setup, before it's replaced
                self.presets["active"] = name
                self._save_presets()  # (a)
                self._put(new)  # (b)
            except Exception:
                # the switch didn't happen: active back to the previous preset, its snapshot dropped (it is the
                # live config), the target keeps its own. If this undo-save fails too, presets.json still shows
                # the switch — the loader finishes it at the next start.
                self.presets["active"] = prev_name
                prev["config"] = None
                try:
                    self._save_presets()
                except OSError:
                    pass
                raise
            target["config"] = None
            self._save_presets()  # (c)
        threading.Thread(target=self.refresh_prices, daemon=True).start()  # prices/models off the request path: the switched-in ids may sit at new hosts

    def delete_preset(self, name: str) -> None:
        with self._rlock:
            if name == self.presets["active"]:
                raise ValueError(f"{name} is active; switch to another preset first")
            self.presets["presets"].remove(self._preset(name))
            self._save_presets()

    def rename_preset(self, old: str, name: str, tagline: str) -> None:
        """Edit any preset's name and tagline, active or not."""
        with self._rlock:
            entry = self._preset(old)
            name, tagline = presets.check_name(name), presets.check_tagline(tagline)
            if name != old and name in self.preset_names():
                raise ValueError(f"a preset named {name} exists")
            entry["name"], entry["tagline"] = name, tagline
            if self.presets["active"] == old:
                self.presets["active"] = name
            self._save_presets()

    def preset_config(self, name: str) -> dict:
        """The config JSON of any preset; the active one's is the live config. KeyError on an unknown name."""
        with self._rlock:
            if name == self.presets["active"]:
                return config_to_json(self.routing)
            return self._preset(name)["config"]

    def preset_meta(self, name: str) -> tuple[str, str]:
        """(name, tagline) of a preset, under the lock. KeyError on an unknown name."""
        with self._rlock:
            p = self._preset(name)
            return p["name"], p["tagline"]


def default_config_dir() -> str:
    return os.path.expanduser(os.environ.get("SIGNALLER_CONFIG_DIR") or "~/.config/signaller")


def make_server(cfg: Config, app: App | None = None) -> http.server.ThreadingHTTPServer:
    app = app or App(cfg, default_config_dir())

    class Handler(http.server.BaseHTTPRequestHandler):
        # ponytail: HTTP/1.0 close-delimited responses, no keep-alive; switch to chunked if connection churn ever matters
        protocol_version = "HTTP/1.0"

        def log_message(self, *a):
            pass

        def _client_gone(self) -> bool:
            """The client hung up or its socket errored. A backstop only (the failed ping write is the real detector):
            any doubt, including select's ValueError on a descriptor of 1024 or more, means not gone."""
            try:
                # hang-up and error only: on Linux a client that half-closes after its request stays (macOS reports
                # a half-close as POLLHUP too)
                if hasattr(select, "poll"):
                    p = select.poll()
                    p.register(self.connection, select.POLLHUP | select.POLLERR | select.POLLNVAL)
                    return bool(p.poll(0))
                # ponytail: no poll (Windows): a readable EOF also reads as gone, a half-close included
                return bool(select.select([self.connection], [], [], 0)[0]) and not self.connection.recv(1, socket.MSG_PEEK)
            except Exception:
                return False

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
                self._error(403, "signaller: forbidden origin/host")
                print("%s route=- reason=forbidden status=403 shadow=-" % time.strftime("%Y-%m-%d %H:%M:%S"),
                      file=sys.stderr, flush=True)
                return
            try:
                n = int(self.headers.get("content-length") or 0)
                if n < 0:
                    raise ValueError
            except ValueError:
                self.close_connection = True
                return self._error(400, "signaller: bad content-length")
            body = self.rfile.read(n)
            routing = app.routing  # read once; a concurrent set_routing doesn't affect this request
            model, stream, req = "-", False, None
            try:
                req = json.loads(body)
                model, stream = str(req.get("model", "-")), bool(req.get("stream"))
            except (ValueError, AttributeError):
                pass
            any_sid = session_of(self.headers, req)
            lever = app.lever_for(any_sid)
            if lever:  # /signaller set this session's own lever
                routing = replace(routing, lever=lever)
            try:  # a bad saved config must answer, not drop the connection; never guess a track
                f = Features.from_body(body, req) if is_routed(self.command, self.path) else None
                msgs = self.path.split("?", 1)[0] == "/v1/messages"  # count_tokens decides but doesn't move auto_last
                labelling = f is not None and msgs and autotrain.is_label_request(req)
                if f is not None:
                    f.session, f.hour = any_sid, app.hour_of(f.turn_key, time.localtime(t0).tm_hour)
                    # Claude Code sends an agent id on every request that isn't the main thread's (the team lead's,
                    # in a team); a subagent also has cc_is_subagent=true, so the rest are teammates
                    f.teammate = not f.subagent and bool(self.headers.get("x-claude-code-agent-id"))
                    if labelling:
                        # the labeller's own request: its prompt text never reaches the store, the console or a shadow
                        f.prompt, f.step = "", None
                if labelling:
                    # the marker says who sent it: straight to Anthropic, whatever the lever or the rules say
                    d = Decision("anthropic", "auto training",
                                 [{"rule": "Auto training", "outcome": "labelling request → anthropic", "decided": True}])
                else:
                    auto = (lambda f: app.auto_decide(f, routing.auto)) if routing.lever == "auto" else None
                    d = decide_route(routing, f, app.classifier, auto=auto) if f else None
                    if d and d.auto and msgs:  # auto decided (or a lead tag did): record it for the console, main thread only
                        app.note_auto(f, *d.auto)
            except Exception as e:
                self._error(500, "signaller: routing failed")
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
                # a labelling request has no shadow: its text goes to Anthropic and nowhere else
                turn_id = app.doubler.turn_for(f, route, routing.doubling, sid) if rec and app.doubler and not labelling else None
                if turn_id:  # shadow starts before the served upstream; it never touches the served path again
                    pair = app.doubler.pending_served(turn_id, rid, route, routing.doubling.shadow[route])
                    shadow = app.doubler.start_shadow(turn_id, rid, f, route, self.command, self.path,
                                                      self.headers.items(), body, app, routing)  # the shadow strips its own tags
            except Exception as e:
                print("doubling failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
                pair, shadow, turn_id = None, "-", None
            orig_body = body  # an image fallback to Anthropic starts again from the request as Claude Code sent it
            stripped = False
            if labelling:
                # untouched in every way: no tag strip, no thinking drop, no settings lines, byte for byte
                pr = Prepared(*upstream(app, "anthropic"), [(k, v) for k, v in self.headers.items() if k.lower() not in HOP],
                             model, None, False, None, None)
            else:
                try:  # routing already read the tags; no upstream needs them
                    stripped = bool(f) and isinstance(req, dict) and strip_lead_tags(req)
                except Exception:
                    pass
                advise = rec and stream and isinstance(req, dict)  # the Advisor runs only in a streamed main answer
                pr = prepare(app, route, model, req, self.headers.items(), routing, f, advise=advise, raw=body)
            if stripped or pr.changed:
                body = json.dumps(req).encode()
            if pr.model != model:
                shown = "%s→%s" % (model, pr.model)
            status, err, ttfb, conn, first, fell = 0, "", None, None, None, None
            images = route != "anthropic" and has_images(req)
            # the row bills every leg as the downstream sent it (each leg re-sends the conversation); the client
            # stream keeps leg 1's input and cache tokens. One capture per leg, raw bytes
            leg_caps, sp = [], None
            adv_ms = [0]  # time spent in advice calls: their own rows carry it, the served row's ms doesn't
            # the columns the served row and its advice rows share; not turn_id: an advice row is never doubled
            context = lambda: {"track": route, "step": f.step, "prompt": f.prompt, "session_id": sid,
                               "preview": ("↳ " + " · ".join(f.step.split("\n")) if f.step else f.prompt)[:120]}

            def run_advice(body_d):
                """One advisor call on the downstream, recorded as its own row: (advice text, failed). Never raises."""
                t1, st, ttfb1, usage1, text, c2, plan = time.time(), 0, None, {}, None, None, pr.plan
                try:
                    ah = [(k, v) for k, v in pr.headers if k.lower() != WINDOW_HEADER.lower()]
                    if plan.window:
                        ah.append((WINDOW_HEADER, plan.window))
                    c2, r2 = open_upstream(pr.base, "POST", self.path, ah, json.dumps(body_d).encode())  # the served path and query
                    st, ttfb1 = r2.status, int((time.time() - t1) * 1000)
                    raw = r2.read(ADVICE_CAP)
                    ac = AnswerCapture(r2.getheader("content-type", ""), r2.getheader("content-encoding", ""))
                    ac.feed(raw)
                    usage1 = ac.result().get("usage") or {}
                    if st == 200:
                        text = advisor.advice_text(json.loads(raw))
                except Exception:
                    pass
                finally:
                    if c2:
                        c2.close()
                if text is None and st in (0, 200):  # no answer, or one that can't be read: an error row like any other
                    st = 502
                ms1 = int((time.time() - t1) * 1000)
                adv_ms[0] += ms1
                try:  # api_cost: what the advisor model Claude Code named would have cost on Anthropic, the savings
                    app.store.record_request(request_row(
                        app, pr.ds, usage1, plan.asked, plan.model, plan.window, st, ttfb1, ms1, **context(),
                        id=app.store.next_id(), ts=t1, reason="advisor", parent_id=rid, stream=0, override=None,
                        explanation=json.dumps([{"rule": "Advisor", "outcome": "asked by #%s → %s" % (rid, plan.model),
                                                 "decided": True}])))
                except Exception:
                    pass
                print("%s route=%s reason=advisor model=%s→%s status=%d ms=%d stream=False%s shadow=-" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), route, plan.asked, plan.model, st, ms1, "" if text else " err=advisor"),
                    file=sys.stderr, flush=True)
                return text or "advisor unavailable", text is None

            def open_leg(body_b):
                """The next leg, connected and its first bytes read: (conn, resp, first chunk), or None when it can't
                be had or isn't a 200 event stream (its connection closed). Never raises: run_pinging runs it."""
                c3 = None
                try:
                    c3, r3 = open_upstream(pr.base, self.command, self.path, pr.headers, body_b)
                    if r3.status == 200 and r3.getheader("content-type", "").startswith("text/event-stream"):
                        return c3, r3, r3.read1(65536)
                except Exception:
                    pass
                if c3:
                    c3.close()
                return None

            def ping(b):
                self.wfile.write(b)
                self.wfile.flush()
            try:
                if route != "anthropic" and not pr.key:  # never fall back to another track
                    status = 502
                    self._error(502, "signaller: %s has no key; set it in the console" % pr.name)
                    return
                try:
                    # every request is tried where it was sent: a rewound or forked conversation may have no image
                    conn, resp = open_upstream(pr.base, self.command, self.path, pr.headers, body)
                    ttfb = int((time.time() - t0) * 1000)
                    if images and resp.status == 400:  # an error body is small and nothing is sent yet: read it
                        first = resp.read()
                        if b"image" in first.lower():
                            fell, first = route, None
                            conn.close()
                    if fell:  # the only fallback: a downstream that can't read the conversation's images
                        route = "anthropic"
                        req2 = json.loads(orig_body)
                        stripped2 = strip_lead_tags(req2)
                        pr = prepare(app, route, model, req2, self.headers.items(), routing, f, raw=orig_body)
                        body, shown = json.dumps(req2).encode() if stripped2 or pr.changed else orig_body, model
                        d.explanation.append({"rule": "Fallback", "outcome": f"{fell} can't take images → anthropic", "decided": True})
                        if sid:
                            app.notices[sid] = {"turn": f.turn_key[:16], "text": f"This turn went to Anthropic, not {fell}: "
                                                f"{fell}'s model can't read the image in this conversation."}
                        if pair:  # the served side of the pair was the downstream, and it didn't answer
                            pair.finish({**AnswerCapture.EMPTY, "status": 400, "ttfb_ms": ttfb, "ms": int((time.time() - t0) * 1000),
                                         "error": "no image input; served by anthropic"})
                            pair = None
                        conn, resp = open_upstream(pr.base, self.command, self.path, pr.headers, body)
                        ttfb = int((time.time() - t0) * 1000)
                except (OSError, http.client.HTTPException) as e:
                    status = 502
                    what = "timed out" if isinstance(e, TimeoutError) else "unreachable"
                    self._error(502, "signaller: %s %s at %s" % (
                        pr.name, what, pr.base))
                    return
                status, ctype = resp.status, resp.getheader("content-type", "")
                # the Advisor offered: the answer goes through a Splicer, which holds back an advisor call to run here
                # (its length changes: close-delimited, no content-length)
                sp = advisor.Splicer() if pr.plan and first is None and status == 200 and ctype.startswith("text/event-stream") else None
                self.send_response(status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP or (k.lower() == "content-length" and not sp):
                        self.send_header(k, v)
                self.end_headers()
                if rec:
                    cap = AnswerCapture(resp.getheader("content-type", ""), resp.getheader("content-encoding", ""))
                if first is not None:  # a 400 read above that wasn't about images: pass it on as it came
                    self.wfile.write(first)
                    if cap:
                        cap.feed(first)

                def send(chunks):
                    for c in chunks:
                        self.wfile.write(c)
                        self.wfile.flush()
                        if cap:  # only after the bytes are out: the capture sees what the client got
                            cap.feed(c)

                cur, calls, adv_err, nxt = req, 0, "", None
                if sp:
                    leg_caps.append(AnswerCapture(ctype))
                while first is None:
                    if nxt is not None:  # the next leg's first bytes, read while pinging
                        chunk, nxt = nxt, None
                    else:
                        try:
                            chunk = resp.read1(65536)
                        except (OSError, http.client.HTTPException):
                            chunk, err = b"", " err=midstream"
                    if chunk:
                        send(sp.feed(chunk) if sp else [chunk])
                        if sp:
                            leg_caps[-1].feed(chunk)
                        continue
                    # a short content-length body ends silently; resp.length is the bytes still owed
                    if resp.length:
                        err = " err=midstream"
                    if not sp:
                        break
                    send(sp.finish())
                    if err or sp.broken or sp.call is None or calls == advisor.MAX_CALLS:  # a normal end: release what's held
                        send(sp.flush())
                        break
                    calls, sent, stage = calls + 1, False, "advisor"
                    try:
                        partial, call_id = sp.partial, sp.call["id"]
                        body_d = advisor.advice_request(cur, partial, pr.plan.model)
                        got, gone = run_pinging(lambda: run_advice(body_d), ping)
                        if gone:
                            break
                        text, failed = got or ("advisor unavailable", True)
                        evs = sp.advice_events(call_id, text, failed)
                        sent = True
                        send(evs)
                        if sp.mixed:  # next to another tool call: Claude Code runs that one, no next leg
                            send(sp.rest())
                            break
                        if self._client_gone():
                            break
                        # the issued id, as the spliced block and every later request carry it: one pair, so prefix caches hit
                        cur = advisor.continue_request(cur, partial, advisor.ISSUED + str(call_id), text, failed,
                                                       calls < advisor.MAX_CALLS)
                        sp.next_leg()
                        conn.close()
                        conn, stage = None, "leg2"  # a failure from here on is the next leg's, not the advice's
                        # pings go on until the next leg's first bytes: a queued window can hold its headers back
                        body_n = json.dumps(cur).encode()
                        got, gone = run_pinging(lambda: open_leg(body_n), ping)
                        if got:
                            conn, resp, nxt = got
                        if gone:  # nothing of the next leg goes out; finally closes it
                            break
                        if not got:
                            raise ValueError("leg %d unavailable" % (calls + 1))
                        leg_caps.append(AnswerCapture(resp.getheader("content-type", "")))
                    except Exception:
                        adv_err = " err=" + stage
                        send([MIDSTREAM_SSE] if sent else sp.flush())
                        break
                if err:  # headers are out, so tell the client the stream is truncated
                    if ctype.startswith("text/event-stream"):
                        self.wfile.write(MIDSTREAM_SSE)
                    else:  # RST, so the client sees a failure rather than a clean EOF
                        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                err = err or adv_err
            except OSError:
                pass  # client went away
            finally:
                if conn:
                    conn.close()
                ms = int((time.time() - t0) * 1000) - adv_ms[0]
                try:  # result() consumes a JSON body: call it once
                    res = cap.result() if cap else {}
                except Exception:
                    res = {}
                usage = res.get("usage") or {}
                if sp:  # the Splicer may have held leg 1's message_delta back from cap: every leg's tokens, summed
                    usage = sum_usage(leg_caps)
                if pair:
                    res = res or AnswerCapture.EMPTY
                    pair.finish({**res, "status": status, "ttfb_ms": ttfb, "ms": ms,
                                 "error": err.strip() or res.get("error")})
                if rec:
                    app.store.record_request(request_row(
                        app, pr.ds, usage, model, pr.model, pr.window, status, ttfb, ms, **context(), turn_id=turn_id, id=rid, ts=t0,
                        reason=d.reason, explanation=json.dumps(d.explanation), stream=int(stream), override=pr.override))
                    f.prompt, f.messages = None, None  # recorded; the preview deque keeps only the capped text, not the history
                    app.recent.append((f, d, rid))
                shown_reason = '"%s"' % reason if re.search(r"\s", reason) else reason
                print("%s route=%s reason=%s model=%s status=%d ms=%d stream=%s%s shadow=%s%s" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), route, shown_reason, shown, status, ms, stream, err, shadow,
                    " fallback=%s" % fell if fell else ""),
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
        print("signaller: %s" % e, file=sys.stderr)
        sys.exit(2)
    try:
        app = App(cfg, default_config_dir())
    except OSError as e:
        print("signaller: config dir unusable: %s" % e, file=sys.stderr)
        sys.exit(2)
    srv = make_server(cfg, app)
    app.start_price_refresh()  # its thread also prunes settings on its first pass
    print("listening on %s:%d; anthropic=%s downstreams=%s" % (
        cfg.listen_host, cfg.listen_port, cfg.anthropic_url,
        ", ".join("%s (%s)" % (d.id, "key set" if app.keys.has(d.id, d.url) else "no key") for d in app.routing.downstreams)),
        file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
