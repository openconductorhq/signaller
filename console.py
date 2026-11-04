"""Console: static page and JSON API under /ui, with loopback Host/Origin checks."""
import json
import os
import re
import time
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, replace
from urllib.parse import parse_qs, unquote, urlsplit

from keys import write_json
from prices import parse_valar
import overrides
from routing import (MAX_DOWNSTREAMS, MODEL_TIERS, WINDOWS, _sid_ok, ClassResult, Rule, untagged, config_from_json, config_to_json,
                     downstream, route, tracks, valid_id)
from rulelang import RuleError, parse

UI_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui")
LOOPBACK_HOST_RE = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", re.I)
MAX_BODY = 1 << 20
BAD = (ValueError, TypeError, OverflowError, RecursionError)
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".svg": "image/svg+xml", ".txt": "text/plain; charset=utf-8"}
SEC = {"Content-Security-Policy": "default-src 'self'", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}
NOT_RE = ClassResult(None, None, "not re-classified")
PALETTE = ("#1F8A5B", "#6B5BD6", "#C8412F", "#8A6D1F", "#1F7A8A", "#A0418A")  # new downstreams, in order, skipping used ones
VALAR_HOST = "api.valarhq.ai"


class Err(Exception):
    def __init__(self, status, msg, **extra):
        self.status, self.body = status, {"error": msg, **extra}


def send(h, status, body, ctype, cache=False):
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    for k, v in SEC.items():
        h.send_header(k, v)
    if not cache:
        h.send_header("Cache-Control", "no-store")
    h.end_headers()
    if h.command != "HEAD":
        h.wfile.write(body)


def send_json(h, status, obj):
    send(h, status, json.dumps(obj).encode(), "application/json")


def preview(app, body):
    t0 = time.time()
    try:
        r, pos = body["rule"], int(body["position"])
        text = str(r["text"])
        parse(text)  # Rule() would hide line/col behind a ValueError
        rule = Rule(str(r["name"]), "dsl", True, text)
    except RuleError as e:
        raise Err(400, e.message, line=e.line, col=e.col)
    except RecursionError:
        raise Err(400, "rule is nested too deeply")
    except (KeyError, TypeError, ValueError, OverflowError) as e:
        raise Err(400, "bad preview request: %s" % e)
    cur = app.routing
    rules = [x for x in cur.rules if x.name != rule.name]
    rules.insert(max(0, min(pos, len(rules))), rule)
    cand = replace(cur, lever="rules", rules=tuple(rules))
    rows, changed = [], 0
    for f, d, rid in list(app.recent):
        stub = lambda _f, _cs, d=d: d.classified or NOT_RE
        try:
            would = route(cand, f, stub)
        except RecursionError:
            raise Err(400, "rule is nested too deeply")
        if would.reason == "rule:" + rule.name or would.reason.startswith("rule:%s:" % rule.name):
            changed += would.track != d.track
            rows.append({"id": rid, "ts": None, "went": d.track, "why": d.reason, "would": would.track, "preview": untagged(f.text)[:120]})
    ids = [x["id"] for x in rows]
    ts = {r["id"]: r["ts"] for r in app.store.departures(limit=200) if r["id"] in ids}
    for x in rows:
        x["ts"] = ts.get(x["id"])
    ms = (time.time() - t0) * 1000
    return {"matched": len(rows), "changed": changed, "rows": rows, "slow": ms > 50,
            "count": len(app.recent)}


def fetch_models(url, key, timeout=5):
    """The upstream's raw /v1/models payload."""
    req = urllib.request.Request(url + "/v1/models", headers={"Authorization": "Bearer " + key})
    nored = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
    with nored.open(req, timeout=timeout) as r:  # a redirect would re-send the key elsewhere
        return json.load(r)


def read_models(config_dir):
    """models.json: the last list each upstream gave us (+ listed_prices), also hand-editable for an upstream that's down."""
    try:
        with open(os.path.join(config_dir, "models.json")) as f:
            saved = json.load(f)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def save_models(config_dir, **updates):
    """Merge updates into models.json; a None value drops that key (a deleted downstream's list)."""
    # ponytail: read-merge-write; a concurrent save can lose the other's keys until the next refresh rewrites them
    saved = read_models(config_dir)
    if all(saved.get(k) == v for k, v in updates.items()):
        return
    write_json(os.path.join(config_dir, "models.json"), {k: v for k, v in {**saved, **updates}.items() if v is not None}, indent=1)


def clean_url(url):
    if not isinstance(url, str):
        raise Err(400, "url must be text")
    return url.strip().rstrip("/").removesuffix("/v1")


def probe(url, key):
    """GET {url}/v1/models (never a message): {ok, reason, models, kind}. The reason never carries the key."""
    u = urlsplit(url)
    out = {"ok": False, "reason": None, "models": [], "kind": "valar" if u.hostname == VALAR_HOST else "generic"}
    if u.scheme not in ("http", "https") or not u.hostname:
        return {**out, "reason": "the url must start with http:// or https://"}
    try:
        payload = fetch_models(url, key)
    except urllib.error.HTTPError as e:
        return {**out, "reason": "HTTP %d" % e.code}
    except urllib.error.URLError as e:  # the socket's reason: no headers in it
        return {**out, "reason": "timed out" if isinstance(e.reason, TimeoutError) else str(e.reason)}
    except TimeoutError:
        return {**out, "reason": "timed out"}
    except ValueError:
        return {**out, "reason": "not a model list (the answer isn't JSON)"}
    except Exception as e:  # type only: some messages quote the request's headers
        return {**out, "reason": type(e).__name__}
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not all(isinstance(m, dict) and isinstance(m.get("id"), str) for m in data):
        return {**out, "reason": "not a model list (no data[].id)"}
    try:
        valar = bool(parse_valar(payload))
    except Exception:
        valar = False
    return {"ok": True, "reason": None, "models": sorted(m["id"] for m in data), "kind": "valar" if valar else out["kind"]}


def add_downstream(app, body):
    """The wizard's save: kind (from a probe), windows and colour are ours to set; a failed probe still saves."""
    if not isinstance(body, dict):
        raise Err(400, "body must be an object")
    cur = app.routing
    err = valid_id(body.get("id"), tracks(cur)[1:])
    if err:
        raise Err(400, err)
    if len(cur.downstreams) >= MAX_DOWNSTREAMS:
        raise Err(409, "at most %d downstreams" % MAX_DOWNSTREAMS)
    url, key = clean_url(body.get("url")), body.get("key") or ""
    if urlsplit(url).scheme not in ("http", "https"):
        raise Err(400, "the url must start with http:// or https://")
    if not isinstance(key, str):
        raise Err(400, "key must be text")
    kind = probe(url, key)["kind"]
    used = {d.color for d in cur.downstreams}
    color = next((c for c in PALETTE if c not in used), PALETTE[len(used) % len(PALETTE)])
    d = {"id": body["id"], "name": body.get("name"), "url": url, "kind": kind, "models": body.get("models"), "color": color,
         "windows": dict.fromkeys(MODEL_TIERS, "asap") if kind == "valar" else None, "prices": body.get("prices")}
    try:
        return asdict(app.add_downstream(d, key))
    except ValueError as e:
        raise Err(409 if str(e).startswith("at most") else 400, str(e))


def forget_models(app, ids):
    """A deleted downstream's saved model list goes too: one re-added under its id must not show it."""
    try:
        save_models(app.config_dir, **dict.fromkeys(ids))
    except OSError:
        pass


def in_use(id, refs):
    return Err(409, "%s is still in use: %s" % (id, "; ".join(refs)), references=refs)


def check_downstream_edits(app, new):
    """A config PUT may edit downstreams (name, url, models, windows, prices), never add or remove one."""
    before = {d.id: d for d in app.routing.downstreams}
    for d in new.downstreams:
        o = before.get(d.id)
        if o is None:
            raise Err(400, "downstream %s is new: add downstreams with the wizard, and ids can't be changed" % d.id)
        for k in ("kind", "color"):
            if getattr(d, k) != getattr(o, k):
                raise Err(400, "downstream %s: %s can't be changed" % (d.id, k))
    if any(downstream(new, i) is None for i in before):
        raise Err(400, "remove downstreams with Delete on the Tracks row")


def windows_of(valar_prices):
    return {m: [w for w in WINDOWS if w in p] for m, p in valar_prices.items() if isinstance(p, dict)}


def filt(query):
    """(sessions|None, t0, t1) from ?sessions=a,b&from=<ts|session>&to=<ts>; 'session' = from the picked sessions' first request."""
    now = time.time()
    ids = [x for x in (query.get("sessions") or [""])[0].split(",") if x] or None
    try:
        f, t = (query.get(k, [None])[0] for k in ("from", "to"))
        t1 = float(t) if t is not None else now
        t0 = float(f) if f not in (None, "session") else None
        if t0 is None and f is None:
            t0 = now - 3600
    except ValueError:
        raise Err(400, "from/to must be numbers or 'session'")
    return ids, t0, t1


def state(app, query=None):
    ids, t0, t1 = filt(query or {})
    if t0 is None:  # whole session
        t0 = app.store.first_ts(ids)
    c, ts = app.cfg, tracks(app.routing)
    return {"config": config_to_json(app.routing), "stats": app.store.stats(time.time(), ts),
            "summary": app.store.summary(ids, t0, t1, time.time(), tracks(app.routing)), "banner": app.banner,
            "uptime_s": int(time.time() - app.started), "listen": "%s:%d" % (c.listen_host, c.listen_port),
            "dropped": app.store.dropped,
            "downstreams": {d.id: {"key_set": app.keys.has(d.id), "url": urlsplit(d.url).netloc} for d in app.routing.downstreams},
            "recent_count": len(app.recent), "session_levers": dict(app.session_levers),
            "collisions": overrides.collisions(app.routing.settings),
            "clock": {"now": time.time(), "tz": time.strftime("%Z"), "hour": time.localtime().tm_hour}}


def api(h, app, method, path, query, body):
    st = app.store
    if path == "/ui/api/state" and method == "GET":
        return state(app, query)
    if path == "/ui/api/models" and method == "GET":
        from claude_router import upstream
        t = (query.get("track") or [""])[0]
        name, url, key, ds = upstream(app, t)
        if not ds:
            raise Err(400, "track must be a downstream")
        saved = read_models(app.config_dir)
        try:
            if not key:
                raise Err(502, "%s not configured" % name)
            try:
                payload = fetch_models(url, key)
                ids = sorted(m["id"] for m in payload["data"])
            except Exception as e:  # reason only: never headers, never the key
                raise Err(502, "%s unreachable: %s" % (name, getattr(e, "reason", None) or type(e).__name__))
        except Err as e:
            if isinstance(saved.get(t), list) and saved[t]:
                out = {"models": [str(m) for m in saved[t]], "stale": e.body["error"]}
                lp = (saved.get("listed_prices") or {}).get(t) or (t == "valar" and saved.get("valar_prices"))
                if ds.kind == "valar" and isinstance(lp, dict):
                    out["windows"] = windows_of(lp)
                return out
            raise
        out, upd = {"models": ids}, {t: ids}
        if ds.kind == "valar":
            try:
                vp = parse_valar(payload)
                if vp:
                    app.prices.set_listed(t, vp)
                    upd["listed_prices"] = app.prices.listed
                out["windows"] = windows_of(vp)
            except Exception:
                pass  # unparseable prices: ids still go out, every window stays offered
        try:
            save_models(app.config_dir, **upd)
        except OSError:
            pass  # no cache is fine; the live list still goes out
        return out
    if path == "/ui/api/departures" and method == "GET":
        q = {k: int(v[0]) for k, v in query.items() if k in ("since", "before")}
        ids, t0, t1 = filt(query)  # the picked span ("whole session": no lower bound) and sessions
        return st.departures(q.get("since"), q.get("before"), 200, ids, t0, t1)
    if path == "/ui/api/sessions" and method == "GET":
        ids, t0, t1 = filt(query)
        return st.list_sessions(t0 if t0 is not None else st.first_ts(None), t1)
    m = re.fullmatch(r"/ui/api/requests/(\d+)", path)
    if m and method == "GET":
        r = st.request(int(m[1]))
        if not r:
            raise Err(404, "no such request")
        try:
            r["explanation"] = json.loads(r["explanation"])
        except (TypeError, ValueError):
            pass
        if r["turn_id"]:
            r["shadow"] = st.pair_for_request(r["id"])
        return r
    if path == "/ui/api/lever":  # one Claude Code session's own lever (/router), over the global one
        body = body if isinstance(body, dict) else {}
        sid = body.get("session") if method == "PUT" else (query.get("session") or [None])[0]
        if not _sid_ok(sid):
            raise Err(400, "session must be a Claude Code session id")
        modes = ["rules", *tracks(app.routing)]
        if method == "PUT":
            lever = body.get("lever")
            if lever is not None and lever not in modes:
                raise Err(400, "lever must be one of %s, or null" % ", ".join(modes))
            app.set_session_lever(sid, lever)
        elif method != "GET":
            raise Err(405, "GET or PUT")
        own = app.session_levers.get(sid)
        return {"lever": own or app.routing.lever, "session": own, "global": app.routing.lever, "modes": modes}
    m = re.fullmatch(r"/ui/api/sessions/([^/]+)", path)
    if m and method == "DELETE":  # the X on a session chip: its history goes; a live session starts again from zero
        sid = unquote(m[1])
        if sid != "unknown" and not _sid_ok(sid):
            raise Err(400, "session must be a Claude Code session id")
        if not st.forget_session(sid):
            raise Err(503, "the stats database is busy; try again")
        app.sessions.forget(sid)
        return {"ok": True}
    if path == "/ui/api/name" and method == "PUT":  # /router name: what the console calls this session
        body = body if isinstance(body, dict) else {}
        sid, name = body.get("session"), body.get("name")
        if not _sid_ok(sid):
            raise Err(400, "session must be a Claude Code session id")
        if name is not None and not isinstance(name, str):
            raise Err(400, "name must be text or null")
        name = " ".join((name or "").split())[:80] or None
        st.set_session_name(sid, name)
        return {"session": sid, "name": name}
    if path == "/ui/api/settings" and method == "PUT":  # one session's window/model overrides on a downstream
        body = body if isinstance(body, dict) else {}
        sid, ds = body.get("session"), body.get("downstream")
        if not _sid_ok(sid):
            raise Err(400, "session must be a Claude Code session id")
        if not downstream(app.routing, ds):
            raise Err(400, "no downstream named %r" % ds)
        reset, tier, key = bool(body.get("reset")), body.get("tier"), body.get("set")
        if not reset:
            if key not in ("window", "model"):
                raise Err(400, "set must be window or model")
            if tier is not None and (not isinstance(tier, str) or tier not in MODEL_TIERS):
                raise Err(400, "tier must be one of " + ", ".join(MODEL_TIERS) + " or null")
        try:
            app.set_settings_for_session(sid, ds, tier, key, body.get("value"), reset=reset)
        except ValueError as e:  # the round-trip inside set_settings_for_session judged the result; nothing was saved
            raise Err(400, str(e))
        low = sid.lower()
        hits = [s for s in app.routing.settings if low in (overrides.is_session_line(s) or frozenset())]
        return {"lines": overrides.settings_to_json(hits), "collisions": overrides.collisions(app.routing.settings)}
    if path == "/ui/api/config" and method == "PUT":
        try:
            new = config_from_json(body)
        except (ValueError, RecursionError) as e:
            raise Err(400, str(e) if isinstance(e, ValueError) else "rule is nested too deeply")
        check_downstream_edits(app, new)
        repriced = {d.id: d.prices for d in new.downstreams} != {d.id: d.prices for d in app.routing.downstreams}
        app.set_routing(new)
        if repriced:  # rows recorded while unpriced get the new prices now, not at the hourly pass
            try:
                st.reprice(app.prices, new.downstreams)
            except Exception as e:
                print("reprice failed: %s" % type(e).__name__, file=sys.stderr, flush=True)
        return config_to_json(new)
    if path == "/ui/api/downstreams/probe" and method == "POST":
        body = body if isinstance(body, dict) else {}
        key = body.get("key") or ""
        if not isinstance(key, str):
            raise Err(400, "key must be text")
        return probe(clean_url(body.get("url")), key)
    if path == "/ui/api/downstreams" and method == "POST":
        return 201, add_downstream(app, body)
    m = re.fullmatch(r"/ui/api/downstreams/([^/]+)(/key)?", path)
    if m and method in ("PUT", "DELETE") and not downstream(app.routing, m[1]):
        raise Err(404, "no downstream named %s" % m[1])
    if m and m[2] and method == "PUT":
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not key.strip():
            raise Err(400, "key must be non-empty text")
        app.keys.set(m[1], key.strip())
        return {"key_set": True}
    if m and not m[2] and method == "DELETE":
        refs = app.delete_downstream(m[1])
        if refs:
            raise in_use(m[1], refs)
        forget_models(app, [m[1]])
        return {"ok": True}
    if path == "/ui/api/preview" and method == "POST":
        return preview(app, body)
    if path == "/ui/api/turns" and method == "GET":
        return st.turns(unjudged_first=True, sessions=filt(query)[0])
    if path == "/ui/api/pairs" and method == "DELETE":
        st.forget_pairs()
        return {"ok": True}
    m = re.fullmatch(r"/ui/api/turns/([^/]+)(/verdict)?", path)
    if m and not m[2] and method == "GET":
        t = st.turn(unquote(m[1]))
        if not t:
            raise Err(404, "no such turn")
        return t
    if m and m[2] and method == "PUT":
        v = body.get("verdict") if isinstance(body, dict) else None
        if not st.set_verdict(unquote(m[1]), v, tracks(app.routing)):
            raise Err(404 if v in tracks(app.routing) + ("same",) else 400, "bad verdict or unknown turn")
        return {"ok": True}
    raise Err(404, "not found")


def static(h, path):
    name = unquote(path[len("/ui"):]).lstrip("/") or "index.html"
    if "\0" in name:
        raise Err(400, "bad path")
    full = os.path.realpath(os.path.join(UI_DIR, name))
    ctype = TYPES.get(os.path.splitext(full)[1])
    if not full.startswith(UI_DIR + os.sep) or not ctype or not os.path.isfile(full):
        return send(h, 404, b"not found", "text/plain; charset=utf-8")
    with open(full, "rb") as f:
        send(h, 200, f.read(), ctype, cache=True)


def handle_ui(h, app):
    try:
        _handle(h, app)
    except Err as e:
        send_json(h, e.status, e.body)
    except BAD as e:
        send_json(h, 400, {"error": "bad request: %s" % type(e).__name__})
    except Exception:  # never drop the connection
        send_json(h, 500, {"error": "internal error"})


def _handle(h, app):
    port = h.server.server_address[1]
    ok = {"http://127.0.0.1:%d" % port, "http://localhost:%d" % port, "http://[::1]:%d" % port}
    origin = h.headers.get("origin")
    if not LOOPBACK_HOST_RE.fullmatch(h.headers.get("host", "")) or \
            (origin is not None and origin not in ok) or h.headers.get("sec-fetch-site", "none") not in ("same-origin", "none") or (h.command in ("PUT", "POST", "DELETE") and origin is None):
        h.close_connection = True  # body left unread: the connection can't carry another request
        raise Err(403, "claude-router: forbidden origin/host")
    n = int(h.headers.get("content-length") or 0)
    if not 0 <= n <= MAX_BODY:
        raise Err(400, "bad content-length")
    raw = h.rfile.read(n) if n else b""
    u = urlsplit(h.path)
    if not u.path.startswith("/ui/api/"):
        if h.command not in ("GET", "HEAD"):
            raise Err(404, "not found")
        return static(h, u.path)
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        raise Err(400, "body is not valid JSON")
    out = api(h, app, h.command, u.path, parse_qs(u.query), body)
    send_json(h, *(out if isinstance(out, tuple) else (200, out)))
