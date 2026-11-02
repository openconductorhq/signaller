"""Console: static page and JSON API under /ui, with loopback Host/Origin checks."""
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import replace
from urllib.parse import parse_qs, unquote, urlsplit

from prices import parse_valar
from routing import WINDOWS, _sid_ok, ClassResult, Rule, config_from_json, config_to_json, route, tracks
from rulelang import RuleError, parse

UI_DIR = os.path.join(os.path.dirname(os.path.realpath(__file__)), "ui")
LOOPBACK_HOST_RE = re.compile(r"(127\.0\.0\.1|localhost|\[::1\])(:\d+)?", re.I)
MAX_BODY = 1 << 20
BAD = (ValueError, TypeError, OverflowError, RecursionError)
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
SEC = {"Content-Security-Policy": "default-src 'self'", "X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff"}
NOT_RE = ClassResult(None, None, "not re-classified")


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
            rows.append({"id": rid, "ts": None, "went": d.track, "why": d.reason, "would": would.track, "preview": f.text[:120]})
    ids = [x["id"] for x in rows]
    ts = {r["id"]: r["ts"] for r in app.store.departures(limit=200) if r["id"] in ids}
    for x in rows:
        x["ts"] = ts.get(x["id"])
    ms = (time.time() - t0) * 1000
    return {"matched": len(rows), "changed": changed, "rows": rows, "slow": ms > 50,
            "since_restart": True, "count": len(app.recent)}


def fetch_models(url, key, timeout=5):
    """The upstream's raw /v1/models payload."""
    req = urllib.request.Request(url + "/v1/models", headers={"Authorization": "Bearer " + key})
    nored = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
    with nored.open(req, timeout=timeout) as r:  # a redirect would re-send the key elsewhere
        return json.load(r)


def list_models(url, key, timeout=5):
    return sorted(m["id"] for m in fetch_models(url, key, timeout)["data"])


def read_models(config_dir):
    """models.json: the last list each upstream gave us (+ valar_prices), also hand-editable for an upstream that's down."""
    try:
        with open(os.path.join(config_dir, "models.json")) as f:
            saved = json.load(f)
        return saved if isinstance(saved, dict) else {}
    except (OSError, ValueError):
        return {}


def save_models(config_dir, **updates):
    # ponytail: read-merge-write; a concurrent save can lose the other's keys until the next refresh rewrites them
    saved = read_models(config_dir)
    if all(saved.get(k) == v for k, v in updates.items()):
        return
    fd, tmp = tempfile.mkstemp(dir=config_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({**saved, **updates}, f, indent=1)
        os.replace(tmp, os.path.join(config_dir, "models.json"))
    except BaseException:
        os.unlink(tmp)
        raise


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
    from claude_router import upstream
    ids, t0, t1 = filt(query or {})
    if t0 is None:  # whole session
        t0 = app.store.first_ts(ids)
    def up(t):
        _, url, key = upstream(app.cfg, t)
        return {"url": urlsplit(url).netloc, "configured": bool(key)}
    c = app.cfg
    return {"config": config_to_json(app.routing), "stats": app.store.stats(time.time()),
            "summary": app.store.summary(ids, t0, t1, time.time()), "banner": app.banner,
            "uptime_s": int(time.time() - app.started), "listen": "%s:%d" % (c.listen_host, c.listen_port),
            "dropped": app.store.dropped,
            "upstreams": {"valar": up("valar"), "local": up("local")},
            "recent_count": len(app.recent), "session_levers": dict(app.session_levers)}


def api(h, app, method, path, query, body):
    st = app.store
    if path == "/ui/api/state" and method == "GET":
        return state(app, query)
    if path == "/ui/api/models" and method == "GET":
        from claude_router import upstream
        t = (query.get("track") or [""])[0]
        if t not in ("valar", "local"):
            raise Err(400, "track must be valar or local")
        name, url, key = upstream(app.cfg, t)
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
                if t == "valar" and isinstance(saved.get("valar_prices"), dict):
                    out["windows"] = windows_of(saved["valar_prices"])
                return out
            raise
        out, upd = {"models": ids}, {t: ids}
        if t == "valar":
            try:
                vp = parse_valar(payload)
                if vp:
                    upd["valar_prices"] = vp
                    app.prices.set_valar(vp)
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
        return st.departures(q.get("since"), q.get("before"), 200, filt(query)[0])
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
        if method == "PUT":
            lever = body.get("lever")
            if lever not in (None, "rules") + tracks(app.routing):
                raise Err(400, "lever must be rules, a track or null")
            app.set_session_lever(sid, lever)
        elif method != "GET":
            raise Err(405, "GET or PUT")
        own = app.session_levers.get(sid)
        return {"lever": own or app.routing.lever, "session": own, "global": app.routing.lever}
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
    if path == "/ui/api/config" and method == "PUT":
        try:
            new = config_from_json(body)
        except (ValueError, RecursionError) as e:
            raise Err(400, str(e) if isinstance(e, ValueError) else "rule is nested too deeply")
        app.set_routing(new)
        return config_to_json(new)
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
        if not st.set_verdict(unquote(m[1]), v):
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
        raise Err(403, "claude-router: forbidden origin/host")  # body left unread
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
    send_json(h, 200, api(h, app, h.command, u.path, parse_qs(u.query), body))
