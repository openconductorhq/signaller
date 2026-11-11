"""The Advisor on a downstream: pure request rewrites, and the Splicer that rewrites the answer stream (no I/O)."""
import copy
import json
import re

from routing import is_advisor

MAX_CALLS = 3
ISSUED = "srvtoolu_sgnl_"  # the id prefix of the advisor blocks the router issues (dropped on the way to Anthropic)
PROMPT = ("You are an advisor to the assistant in this conversation, a stronger model it consulted mid-task. "
          "You can't run tools. Read the conversation and the assistant's work so far, then advise it on what to do "
          "next: what it may have missed or got wrong, the risks in its current approach, and a concrete next step. "
          "Be brief and specific. Don't redo its work, and don't address the user.")
TOOL = {"name": "advisor",
        "description": "Ask a stronger model for advice on the task: what you may be missing and what to do next. "
                       "Takes no input; it reads the whole conversation.",
        "input_schema": {"type": "object", "properties": {}}}


def definition(req: dict):
    """The Advisor tool definition in the request, or None."""
    for t in req.get("tools") or []:
        if isinstance(t, dict) and is_advisor(t):
            return t
    return None


def strip_advisor(req: dict) -> bool:
    """Every downstream: drop the Advisor tool definition and its calls and results from the history
    (server_tool_use named advisor, advisor_tool_result): a downstream can't run it and rejects the types.
    A message the drop leaves empty goes too. True if anything changed."""
    tools = req.get("tools")
    changed = False
    if isinstance(tools, list) and any(isinstance(t, dict) and is_advisor(t) for t in tools):
        req["tools"] = [t for t in tools if not (isinstance(t, dict) and is_advisor(t))]
        changed = True
    adv = lambda b: isinstance(b, dict) and (b.get("type") == "advisor_tool_result"
                                             or (b.get("type") == "server_tool_use" and b.get("name") == "advisor"))
    msgs = req.get("messages")
    for m in msgs if isinstance(msgs, list) else []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(adv(b) for b in c):
            m["content"], changed = [b for b in c if not adv(b)], True
    if changed and isinstance(msgs, list):
        # ponytail: an emptied message is dropped, which can leave two same-role messages side by side
        req["messages"] = [m for m in msgs if not (isinstance(m, dict) and m.get("content") == [])]
    return changed


def drop_issued(req: dict) -> bool:
    """On the way to Anthropic: drop the advisor pairs the router issued (ids starting ISSUED), which Anthropic
    never did, and any message the drop leaves empty. Anthropic's own pairs stay. True if anything changed."""
    ours = lambda b: isinstance(b, dict) and (
        (_is_call(b) and str(b.get("id")).startswith(ISSUED))
        or (_is_result(b) and str(b.get("tool_use_id")).startswith(ISSUED)))
    msgs, changed = req.get("messages"), False
    for m in msgs if isinstance(msgs, list) else []:
        c = m.get("content") if isinstance(m, dict) else None
        if isinstance(c, list) and any(ours(b) for b in c):
            m["content"], changed = [b for b in c if not ours(b)], True
    if changed:
        # ponytail: an emptied message is dropped, which can leave two same-role messages side by side
        req["messages"] = [m for m in msgs if not (isinstance(m, dict) and m.get("content") == [])]
    return changed


def _is_call(b) -> bool:
    return isinstance(b, dict) and b.get("type") == "server_tool_use" and b.get("name") == "advisor"


def _is_result(b) -> bool:
    return isinstance(b, dict) and b.get("type") == "advisor_tool_result"


def _id(v) -> bool:
    return isinstance(v, str) and bool(v)


def _result_block(b: dict) -> dict:
    c = b.get("content")
    kind = c.get("type") if isinstance(c, dict) else None
    if kind == "advisor_result":
        text, err = c.get("text") or "", False
    elif kind == "advisor_redacted_result":
        text, err = "(advice redacted)", False
    else:
        text, err = "advisor unavailable", True
    out = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "content": text}
    if err:
        out["is_error"] = True
    return out


def to_client_form(req: dict) -> bool:
    """Swap the Advisor definition for TOOL and split each call/result pair into a client round trip:
    assistant [..., tool_use], user [tool_result], assistant [rest]. A tool call before the advisor's moves to
    after the round trip, so the next user message's result still answers the message before it. Orphans,
    pairs without an id and empty messages are dropped."""
    changed = False
    tools = req.get("tools")
    if isinstance(tools, list) and any(isinstance(t, dict) and is_advisor(t) for t in tools):
        req["tools"] = [TOOL if (isinstance(t, dict) and is_advisor(t)) else t for t in tools]
        changed = True
    msgs = req.get("messages")
    if not isinstance(msgs, list):
        return changed
    out = []
    for m in msgs:
        c = m.get("content") if isinstance(m, dict) else None
        if not (isinstance(c, list) and any(_is_call(b) or _is_result(b) for b in c)):
            out.append(m)
            continue
        changed = True
        results = {b.get("tool_use_id"): b for b in c if _is_result(b) and _id(b.get("tool_use_id"))}
        cur = []
        for b in c:
            if _is_result(b):
                continue
            if _is_call(b):
                r = results.get(b.get("id")) if _id(b.get("id")) else None
                if r is None:
                    continue
                calls = [x for x in cur if isinstance(x, dict) and x.get("type") == "tool_use"]
                out.append({**m, "content": [x for x in cur if x not in calls] +
                            [{"type": "tool_use", "id": b["id"], "name": "advisor", "input": {}}]})
                out.append({"role": "user", "content": [_result_block(r)]})
                cur = calls
            else:
                cur.append(b)
        out.append({**m, "content": cur})
    req["messages"] = [m for m in out if not (isinstance(m, dict) and m.get("content") == [])]
    return changed


def advice_request(req: dict, partial: list, model: str) -> dict:
    """The body for the advice call: the conversation, the work so far (less its tool calls: nothing answers
    them here), then PROMPT. No thinking."""
    body = copy.deepcopy(req)
    msgs = list(body.get("messages") or [])
    work = [b for b in partial if not (isinstance(b, dict) and b.get("type") == "tool_use")]
    if work:
        msgs.append({"role": "assistant", "content": copy.deepcopy(work)})
    msgs.append({"role": "user", "content": [{"type": "text", "text": PROMPT}]})
    body.update(messages=msgs, model=model, max_tokens=4096, stream=False, tool_choice={"type": "none"})
    body.pop("thinking", None)
    return body


def continue_request(req: dict, partial: list, call_id: str, advice: str, error: bool, offer: bool) -> dict:
    """The request that carries on after an advisor call: the call and its result appended; TOOL dropped unless offered."""
    body = copy.deepcopy(req)
    msgs = list(body.get("messages") or [])
    msgs.append({"role": "assistant", "content": copy.deepcopy(partial) + [
        {"type": "tool_use", "id": call_id, "name": "advisor", "input": {}}]})
    res = {"type": "tool_result", "tool_use_id": call_id, "content": advice}
    if error:
        res["is_error"] = True
    msgs.append({"role": "user", "content": [res]})
    body["messages"] = msgs
    if not offer and isinstance(body.get("tools"), list):
        body["tools"] = [t for t in body["tools"] if not (isinstance(t, dict) and t.get("name") == "advisor")]
    return body


def advice_text(body: dict) -> str:
    """The text blocks of an advice answer joined, or a placeholder."""
    t = "\n".join(b.get("text", "") for b in body.get("content") or []
                  if isinstance(b, dict) and b.get("type") == "text" and b.get("text"))
    return t.strip() or "(no advice)"


_BOUNDARY = re.compile(rb"\r?\n\r?\n")


def _in_field(f) -> bool:
    return f == "input_tokens" or str(f).startswith("cache_")


def _sse(ev: dict) -> bytes:
    return b"event: %s\ndata: %s\n\n" % (str(ev.get("type")).encode(), json.dumps(ev).encode())


class Splicer:
    """Rewrites a downstream's SSE answer for Claude Code around an advisor call. No I/O; feed never raises.
    A tool_use named advisor is held back with everything after it; advice_events() stands in for it, and the
    next leg (after next_leg()) has its message_start dropped and its indices shifted by .offset."""

    def __init__(self):
        self.buf, self.broken, self.offset, self.out_tokens = b"", False, 0, 0
        self._base, self._first = 0, True  # output tokens of earlier legs; is this the first leg
        self._in_usage = {}                # leg 1's message_start input and cache usage fields
        self._reset()

    def _reset(self):
        self.call, self.mixed = None, False
        self._blocks, self._json = {}, {}  # leg index -> block before the call / its partial_json pieces
        self._adv = self._adv_id = None    # the advisor tool_use's leg index and id
        self._tools, self._top = 0, -1     # tool_use blocks in the message; highest leg index seen
        self._held = []                    # (bytes as they'd go out, event as parsed or None, leg index or None)

    def next_leg(self):
        self._base, self._first, self.buf = self.out_tokens, False, b""
        self._reset()

    def feed(self, chunk: bytes) -> list:
        if self.broken:
            return [chunk] if chunk else []
        out = []
        try:
            self.buf += chunk
            while True:
                m = _BOUNDARY.search(self.buf)
                if not m:
                    break
                b, ev, idx = self._event(self.buf[:m.end()])
                self.buf = self.buf[m.end():]
                if b is None:
                    continue
                if self._adv is not None:
                    self._held.append((b, ev, idx))
                else:
                    out.append(b)
        except Exception:  # a break: everything passes as it came, and no call is left to run
            self.broken, self.call, self.mixed = True, None, False
            out += [h[0] for h in self._held]
            if self.buf:
                out.append(self.buf)
            self._held, self.buf = [], b""
        return out

    def _event(self, raw: bytes):
        """(bytes to send, or None to drop; the event as parsed; its leg index). Raises on bad JSON."""
        data = [ln[5:] for ln in raw.replace(b"\r", b"").split(b"\n") if ln.startswith(b"data:")]
        if not data or b"\n".join(data).strip() == b"[DONE]":  # a comment, or an OpenAI-style end sentinel
            return raw, None, None
        ev = json.loads(b"\n".join(data))
        if not isinstance(ev, dict):
            return raw, None, None
        k, i = ev.get("type"), ev.get("index")
        if k == "message_start":
            if self._first:
                u = (ev.get("message") or {}).get("usage")
                self._in_usage = {f: v for f, v in u.items() if _in_field(f)} if isinstance(u, dict) else {}
            return (raw if self._first else None), ev, None
        if k == "message_delta":
            u = ev.get("usage")
            if isinstance(u, dict):
                new = dict(u)
                if isinstance(u.get("output_tokens"), int):
                    self.out_tokens = new["output_tokens"] = self._base + u["output_tokens"]
                if not self._first:  # input and cache tokens stay leg 1's: replaced, or dropped if leg 1 had none
                    for f in [f for f in new if _in_field(f)]:
                        if f in self._in_usage:
                            new[f] = self._in_usage[f]
                        else:
                            del new[f]
                if new != u:
                    raw = _sse({**ev, "usage": new})
            if (ev.get("delta") or {}).get("stop_reason") == "tool_use" and self._adv is not None:
                self.call, self.mixed = {"id": self._adv_id}, self._tools > 1
            return raw, ev, None
        if not (isinstance(k, str) and k.startswith("content_block_") and isinstance(i, int)):
            return raw, ev, None
        self._top = max(self._top, i)
        if k == "content_block_start":
            cb = ev.get("content_block") or {}
            if cb.get("type") == "tool_use":
                self._tools += 1
                if cb.get("name") == "advisor" and self._adv is None:
                    self._adv, self._adv_id = i, str(cb.get("id"))
            if self._adv is None:
                self._blocks[i] = copy.deepcopy(cb)
        elif k == "content_block_delta" and i in self._blocks and self._adv is None:
            b, d = self._blocks[i], ev.get("delta") or {}
            for dt, key in (("text_delta", "text"), ("thinking_delta", "thinking"), ("signature_delta", "signature")):
                if d.get("type") == dt and isinstance(d.get(key), str):
                    b[key] = (b.get(key) or "") + d[key]
            if d.get("type") == "input_json_delta" and isinstance(d.get("partial_json"), str):
                self._json.setdefault(i, []).append(d["partial_json"])
        if self.offset:
            raw = _sse({**ev, "index": i + self.offset})
        return raw, ev, i

    @property
    def partial(self) -> list:
        """This leg's content blocks before the advisor call, rebuilt from the deltas."""
        out = []
        for i in sorted(self._blocks):
            b = copy.deepcopy(self._blocks[i])
            if b.get("type") == "tool_use" and i in self._json:
                try:
                    b["input"] = json.loads("".join(self._json[i]) or "{}")
                except ValueError:
                    pass
            if not (b.get("type") == "text" and not b.get("text")):  # strict APIs reject an empty text block
                out.append(b)
        return out

    def advice_events(self, call_id: str, text, error: bool) -> list:
        """server_tool_use + advisor_tool_result at the next two indices; the next leg's offset moves past them."""
        at = self.offset + (self._adv if self._adv is not None else self._top + 1)
        sid = ISSUED + str(call_id)
        content = ({"type": "advisor_tool_result_error", "error_code": "unavailable"} if error else
                   {"type": "advisor_result", "text": text or "", "stop_reason": "end_turn"})
        self.offset = at + 2
        return [_sse({"type": "content_block_start", "index": at,
                      "content_block": {"type": "server_tool_use", "id": sid, "name": "advisor", "input": {}}}),
                _sse({"type": "content_block_stop", "index": at}),
                _sse({"type": "content_block_start", "index": at + 1,
                      "content_block": {"type": "advisor_tool_result", "tool_use_id": sid, "content": content}}),
                _sse({"type": "content_block_stop", "index": at + 1})]

    def rest(self) -> list:
        """After advice_events on a mixed call: the other held blocks shifted past the advice, then the tail."""
        out = []
        for b, ev, j in self._held:
            if j is None:
                out.append(b)
            elif j != self._adv:
                out.append(_sse({**ev, "index": j - self._adv - 1 + self.offset}))
        self._held = []
        return out

    def finish(self) -> list:
        """At a leg's end: a last event that came without its closing blank line, read as if it had one."""
        out = self.feed(b"\n\n") if self.buf.strip() else []
        self.buf = b""
        return out

    def flush(self) -> list:
        """The held events as they'd have gone out, for a call that isn't run."""
        out, self._held = [h[0] for h in self._held], []
        return out
