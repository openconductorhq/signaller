"""Doubling: send a copy of a request to the other track in the background and store both answers as a pair."""
import json
import random
import threading
import time
import uuid

TEXT_CAP, INPUT_CAP = 50_000, 2000
TURN_TTL = 30 * 60


class AnswerCapture:
    """Collects text, tool calls and output tokens from a response, fed in arbitrary chunks. Never raises."""

    def __init__(self, content_type: str):
        self.sse = (content_type or "").startswith("text/event-stream")
        self.buf, self.text, self.tools, self.by_index, self.output_tokens = b"", [], [], {}, None
        self.size = 0

    def feed(self, chunk: bytes) -> None:
        if not self.sse:
            self.buf += chunk
            return
        self.buf += chunk
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

    def _event(self, e):
        k = e.get("type")
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
            except Exception:
                pass
        tokens = self.output_tokens if isinstance(self.output_tokens, int) else None
        return {"text": "".join(self.text), "output_tokens": tokens,
                "tools": [{"name": t["name"], "input": "".join(t["input"])[:INPUT_CAP]} for t in self.tools]}


class PairHandle:
    """Joins the served side and the shadow side; whichever arrives second writes the pair."""

    def __init__(self, store, turn_id, request_id, step, served_track):
        self.store, self.lock, self.sides = store, threading.Lock(), {}
        self.row = {"turn_id": turn_id, "request_id": request_id, "step": step, "served_track": served_track,
                    "shadow_track": "local" if served_track == "anthropic" else "anthropic"}

    def _side(self, name, value):
        with self.lock:
            self.sides[name] = value
            if len(self.sides) < 2:
                return
        self.store.record_pair({**self.row, "served": json.dumps(self.sides["served"]),
                                "shadow": json.dumps(self.sides["shadow"])})

    def finish(self, served: dict) -> None:
        self._side("served", served)

    def shadow(self, result: dict) -> None:
        self._side("shadow", result)


class Doubler:
    def __init__(self, store, open_upstream, rng: random.Random | None = None):
        self.store, self.open_upstream, self.rng = store, open_upstream, rng or random.Random()
        self.lock = threading.Lock()
        self.turns = {}  # turn_key -> [turn_id | None, last_seen, steps, recorded]
        self.by_id = {}  # turn_id -> same entry
        self.pending = {}  # request_id -> PairHandle, until start_shadow takes it
        self.caps = {"local": threading.BoundedSemaphore(1), "anthropic": threading.BoundedSemaphore(2)}
        self.inflight = {"local": 0, "anthropic": 0}

    def turn_for(self, f, served_track: str, d) -> str | None:
        if d.rate <= 0 or (d.tag and d.tag.lower() not in f.tags):
            return None
        now = time.time()
        with self.lock:
            # ponytail: sweeps every call; fine for the few hundred turns 30 minutes holds
            for k in [k for k, e in self.turns.items() if now - e[1] > TURN_TTL]:
                self.by_id.pop(self.turns.pop(k)[0], None)
            e = self.turns.get(f.turn_key)
            if e is None:
                tid = uuid.uuid4().hex if self.rng.random() < d.rate else None
                e = self.turns[f.turn_key] = [tid, now, 0, False]
                if tid:
                    self.by_id[tid] = e
            e[1] = now
            if not e[0] or not (d.when_local if served_track == "local" else d.when_anthropic):
                return None
            if not e[3]:
                e[3] = True
                self.store.record_turn({"id": e[0], "ts": now, "preview": f.text[:120]})
            return e[0]

    def pending_served(self, turn_id, request_id, step, served_track) -> PairHandle:
        """step None = next step of the turn."""
        with self.lock:
            if step is None:
                e = self.by_id.get(turn_id)
                if e:
                    e[2] += 1
                step = e[2] if e else 1
            h = self.pending[request_id] = PairHandle(self.store, turn_id, request_id, step, served_track)
        return h

    def start_shadow(self, turn_id, request_id, f, served_track, method, path, headers, body, cfg) -> str:
        """Starts the shadow in a daemon thread; returns its track, or "busy" when that direction is full."""
        with self.lock:
            h = self.pending.pop(request_id, None) or PairHandle(self.store, turn_id, request_id, 1, served_track)
        track = h.row["shadow_track"]
        if not self.caps[track].acquire(blocking=False):
            h.shadow({"skipped": "busy", "status": None, "ttfb_ms": None, "ms": None, "error": None,
                      "text": "", "tools": [], "output_tokens": None})
            return "busy"
        with self.lock:
            self.inflight[track] += 1
        headers = list(headers)  # credentials stay in memory only, in this thread
        threading.Thread(target=self._run, args=(h, track, method, path, headers, body, cfg), daemon=True).start()
        return track

    def _run(self, h, track, method, path, headers, body, cfg):
        from claude_router import HOP, local_headers, local_model  # late: claude_router imports this module
        t0, out, conn = time.time(), {"status": None, "ttfb_ms": None, "error": None}, None
        cap = AnswerCapture("application/json")
        try:
            req = json.loads(body)
            req["stream"] = False
            if track == "local":
                req["model"] = local_model(str(req.get("model", "")), cfg.local_sonnet, cfg.local_haiku)
                headers = local_headers(headers, cfg.litellm_key)
            else:
                headers = [(k, v) for k, v in headers if k.lower() not in HOP]
            conn, resp = self.open_upstream(cfg, track, method, path, headers, json.dumps(req).encode())
            out["ttfb_ms"], out["status"] = int((time.time() - t0) * 1000), resp.status
            cap.feed(resp.read())
        except Exception as e:
            out["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        finally:
            if conn:
                conn.close()
            with self.lock:
                self.inflight[track] -= 1
            self.caps[track].release()
        out["ms"] = int((time.time() - t0) * 1000)
        h.shadow({**out, **cap.result()})

    def active(self) -> dict:
        with self.lock:
            return dict(self.inflight)
