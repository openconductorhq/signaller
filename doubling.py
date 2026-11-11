"""Doubling: send a copy of a request to the other track in the background and store both answers as a pair."""
import json
import random
import threading
import time
import uuid
import zlib

from routing import strip_lead_tags

TEXT_CAP, INPUT_CAP = 50_000, 2000
TURN_TTL = 30 * 60


class NotConfigured(Exception):
    pass


class AnswerCapture:
    """Collects text, tool calls and output tokens from a response, fed in arbitrary chunks. Never raises."""
    EMPTY = {"text": "", "tools": [], "output_tokens": None}  # result() with nothing captured; never mutated

    def __init__(self, content_type: str, content_encoding: str = ""):
        self.sse = (content_type or "").startswith("text/event-stream")
        self.buf, self.text, self.tools, self.by_index, self.output_tokens = b"", [], [], {}, None
        self.usage = dict.fromkeys(("in", "out", "cache_read", "cache_write"))
        self.size, self.error, self.zlib = 0, None, None
        enc = (content_encoding or "").strip().lower()
        if enc in ("gzip", "x-gzip", "deflate"):
            self.zlib = zlib.decompressobj(wbits=47)  # 47: auto-detect gzip or zlib header
        elif enc not in ("", "identity"):
            self.error = "encoded %s" % enc

    def feed(self, chunk: bytes) -> None:
        if self.error:
            return
        if self.zlib:
            try:
                chunk = self.zlib.decompress(chunk)
            except zlib.error:
                self.error = "bad compressed data"
                return
        self.buf += chunk
        if not self.sse:
            return
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            if line.startswith(b"data:"):
                try:
                    self._event(json.loads(line[5:]))
                except Exception:
                    pass

    def _add_text(self, t):
        if isinstance(t, str) and self.size < TEXT_CAP:
            self.text.append(t[:TEXT_CAP - self.size])
            self.size += len(self.text[-1])

    def _usage(self, u):
        """Later events win. Valar models disagree on where the real counts go: Kimi sends zeros in message_start,
        DeepSeek sends the cache-inclusive total there; both send the final split in message_delta. GLM sends
        everything in message_start and only output_tokens in message_delta."""
        for name, key in (("in", "input_tokens"), ("out", "output_tokens"),
                          ("cache_read", "cache_read_input_tokens"), ("cache_write", "cache_creation_input_tokens")):
            if isinstance(u.get(key), int):
                self.usage[name] = u[key]
        cc = u.get("cache_creation")
        if isinstance(cc, dict) and isinstance(cc.get("ephemeral_1h_input_tokens"), int):
            self.usage["cache_write_1h"] = cc["ephemeral_1h_input_tokens"]

    def _event(self, e):
        k = e.get("type")
        if k == "message_start":
            self._usage(e["message"]["usage"])
        if k == "content_block_start" and e["content_block"].get("type") == "tool_use":
            self.by_index[e.get("index")] = tool = {"name": str(e["content_block"].get("name")), "input": []}
            self.tools.append(tool)
        elif k == "content_block_delta":
            dl = e["delta"]
            if dl.get("type") == "text_delta":
                self._add_text(dl.get("text"))
            elif dl.get("type") == "input_json_delta":
                tool = self.by_index.get(e.get("index")) or (self.tools[-1] if self.tools else None)
                if tool is not None and isinstance(dl.get("partial_json"), str):
                    tool["input"].append(dl["partial_json"])
        elif k == "message_delta":
            self.output_tokens = e["usage"]["output_tokens"]
            self._usage(e["usage"])

    def result(self) -> dict:
        if not self.sse and self.buf:
            try:
                obj, self.buf = json.loads(self.buf), b""
                for b in obj.get("content") or []:
                    if b.get("type") == "text":
                        self._add_text(b.get("text"))
                    elif b.get("type") == "tool_use":
                        self.tools.append({"name": str(b.get("name")), "input": [json.dumps(b.get("input"))]})
                self.output_tokens = (obj.get("usage") or {}).get("output_tokens")
                self._usage(obj["usage"])
            except Exception:
                pass
        tokens = self.output_tokens if isinstance(self.output_tokens, int) else None
        return {"error": self.error, "text": "".join(self.text), "output_tokens": tokens, "usage": dict(self.usage),
                "tools": [{"name": t["name"], "input": "".join(t["input"])[:INPUT_CAP]} for t in self.tools]}


class PairHandle:
    """Joins the served side and the shadow side; whichever arrives second writes the pair."""

    def __init__(self, store, turn_id, request_id, step, served_track, shadow_track):
        self.store, self.lock, self.sides = store, threading.Lock(), {}
        self.row = {"turn_id": turn_id, "request_id": request_id, "step": step, "served_track": served_track,
                    "shadow_track": shadow_track}

    def _side(self, name, value):
        with self.lock:
            self.sides[name] = value
            if len(self.sides) < 2:
                return
        self.store.record_pair({**self.row, "served": json.dumps(self.sides["served"]),
                                "shadow": json.dumps(self.sides["shadow"]), "shadow_cost": self.sides["shadow"].get("cost")})

    def finish(self, served: dict) -> None:
        self._side("served", served)

    def shadow(self, result: dict) -> None:
        self._side("shadow", result)


class Doubler:
    def __init__(self, store, open_upstream, rng: random.Random | None = None, prices=None):
        self.store, self.open_upstream, self.rng, self.prices = store, open_upstream, rng or random.Random(), prices
        self.lock = threading.Lock()
        self.turns = {}  # turn_key -> [turn_id | None, last_seen, steps, recorded]
        self.by_id = {}  # turn_id -> same entry
        self.pending = {}  # request_id -> PairHandle, until start_shadow takes it
        self.caps, self.inflight = {}, {}  # track -> BoundedSemaphore(2) / running shadows; made on first use

    def turn_for(self, f, served_track: str, d, session_id=None) -> str | None:
        if d.rate <= 0 or (d.tag and d.tag.lower() not in f.tags):
            return None
        now = time.time()
        with self.lock:
            # ponytail: sweeps every call; fine for the few hundred turns 30 minutes holds
            for k in [k for k, e in self.turns.items() if now - e[1] > TURN_TTL]:
                self.by_id.pop(self.turns.pop(k)[0], None)
            k = (session_id, f.turn_key)  # the same prompt in two sessions is two turns
            e = self.turns.get(k)
            if e is None:
                tid = uuid.uuid4().hex if self.rng.random() < d.rate else None
                e = self.turns[k] = [tid, now, 0, False]
                if tid:
                    self.by_id[tid] = e
            e[1] = now
            if not e[0] or d.shadow.get(served_track) is None:
                return None
            if not e[3]:
                e[3] = True
                self.store.record_turn({"id": e[0], "ts": now, "preview": f.prompt[:120], "session_id": session_id})
            return e[0]

    def pending_served(self, turn_id, request_id, served_track, shadow_track) -> PairHandle:
        """The pair for the turn's next step."""
        with self.lock:
            e = self.by_id.get(turn_id)
            if e:
                e[2] += 1
            step = e[2] if e else 1
            h = self.pending[request_id] = PairHandle(self.store, turn_id, request_id, step, served_track, shadow_track)
        return h

    def start_shadow(self, turn_id, request_id, f, served_track, method, path, headers, body, app, routing=None) -> str:
        """Starts the shadow in a daemon thread; returns its track, or "busy" when that direction is full."""
        with self.lock:
            h = self.pending.pop(request_id)  # pending_served always comes first
            track = h.row["shadow_track"]
            cap = self.caps.setdefault(track, threading.BoundedSemaphore(2))
        if not cap.acquire(blocking=False):
            h.shadow({**AnswerCapture.EMPTY, "skipped": "busy", "status": None, "ttfb_ms": None, "ms": None, "error": None})
            return "busy"
        with self.lock:
            self.inflight[track] = self.inflight.get(track, 0) + 1
        headers = list(headers)  # credentials stay in memory only, in this thread
        try:
            threading.Thread(target=self._run, args=(h, track, method, path, headers, body, app, routing, f), daemon=True).start()
        except Exception as e:  # e.g. can't start new thread: give the slot back, the served request goes on
            self._release(track)
            h.shadow({**AnswerCapture.EMPTY, "skipped": "error", "status": None, "ttfb_ms": None, "ms": None,
                      "error": type(e).__name__})
            return "-"
        return track

    def _release(self, track):
        with self.lock:
            self.inflight[track] -= 1
        self.caps[track].release()

    def _run(self, h, track, method, path, headers, body, app, routing=None, f=None):
        from signaller import prepare  # late: signaller imports this module
        t0, out, conn, cap = time.time(), {"status": None, "ttfb_ms": None, "error": None}, None, None
        model_out, window, ds = None, None, None
        try:
            req = json.loads(body)
            req["stream"] = False
            strip_lead_tags(req)
            name, base, key, ds, headers, model_out, window, _, _, _ = prepare(app, track, str(req.get("model", "")), req, headers, routing, f)
            if track != "anthropic" and not key:
                raise NotConfigured("%s has no key" % name)
            if window:
                out["window"] = window
            conn, resp = self.open_upstream(base, method, path, headers, json.dumps(req).encode())
            out["ttfb_ms"], out["status"] = int((time.time() - t0) * 1000), resp.status
            cap = AnswerCapture(resp.getheader("content-type", ""), resp.getheader("content-encoding", ""))
            cap.feed(resp.read())
        except Exception as e:
            out["error"] = str(e) if isinstance(e, NotConfigured) else "%s: %s" % (type(e).__name__, str(e)[:200])
        finally:
            if conn:
                conn.close()
            self._release(track)
        out["ms"] = int((time.time() - t0) * 1000)
        res = cap.result() if cap else AnswerCapture.EMPTY
        try:
            out["cost"] = self.prices.cost(ds, model_out, window, res.get("usage") or {}) if self.prices else None
        except Exception:
            out["cost"] = None
        h.shadow({**res, **out, "error": out["error"] or res.get("error")})

    def active(self) -> dict:
        with self.lock:
            return dict(self.inflight)
