"""Windows, size estimate and refusal body for compacting before a downstream model's window.

Pure helpers: no I/O, no state beyond `Anchors`' and `Refusals`' in-memory maps. signaller.py wires
these into the request path; nothing here knows about requests.
"""

import json
import threading
import time

from autophase import BoundedDict

# ponytail: the compaction trigger is the smaller of 90% of the window and the window minus the request's
# max_tokens (Valar's context_length and vLLM-style servers count prompt plus output against it), never below
# half the window (a huge or bogus max_tokens cannot make everything refuse) — fixed, not configurable.
LIMIT = 0.9

# ponytail: a flat per-image guess — real image cost depends on the dimensions, but a screenshot is a few
# thousand tokens, not the bytes/4 the raw base64 would count as.
IMAGE_COST_BYTES = 6000

WINDOW_KEYS = ("context_length", "max_input_tokens", "max_model_len")

FLOOR = 1000  # a window below this is nonsense (a broken list or a hand-edit): treated as not listed
CEILING = 10_000_000  # and above this: the manual-window range routing validates a config against


def _window_int(v) -> bool:
    """A value that may act as a context window: an int from FLOOR to CEILING (the range routing
    validates a manual window against at config time)."""
    return isinstance(v, int) and not isinstance(v, bool) and FLOOR <= v <= CEILING


def _entries(payload: dict):
    """A models payload's `data` entries that are dicts: a malformed payload or entry yields nothing."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                yield entry


def listed_windows(payload: dict) -> dict:
    """Windows from a downstream's `/v1/models` payload: per model id, the first value of
    `context_length`, `max_input_tokens`, `max_model_len` that is a window (an int from FLOOR to
    CEILING). Models with none are omitted. Malformed entries are skipped, never raised."""
    out = {}
    for entry in _entries(payload):
        mid = entry.get("id")
        if not isinstance(mid, str):
            continue
        for k in WINDOW_KEYS:
            if _window_int(entry.get(k)):
                out[mid] = entry[k]
                break
    return out


def model_info_windows(payload: dict) -> dict:
    """Windows from LiteLLM's `/model/info`: `data[].model_name` →
    `model_info.max_input_tokens` (a window: an int from FLOOR to CEILING),
    the smallest when a name repeats."""
    out = {}
    for entry in _entries(payload):
        name = entry.get("model_name")
        info = entry.get("model_info")
        v = info.get("max_input_tokens") if isinstance(info, dict) else None
        if not isinstance(name, str) or not _window_int(v):
            continue
        if name not in out or v < out[name]:
            out[name] = v
    return out


def window_for(ds, model: str, listed: dict):
    """The window signaller acts on for this model: the downstream's manual window
    if set, else the listed one, else None (no check)."""
    context = getattr(ds, "context", None)
    if isinstance(context, dict):
        v = context.get(model)
        if _window_int(v):
            return v
    # a hand-edited models.json can give a listed value of any kind: only an int from FLOOR to CEILING is a window
    v = listed.get(ds.id, {}).get(model) if isinstance(listed, dict) else None
    return v if _window_int(v) else None


def _image_bytes(req) -> tuple[int, int]:
    """(bytes of base64 image data, image count) in a request's message content, images nested in
    `tool_result` content lists included. A malformed block is skipped, never raised."""
    n = b = 0

    def walk(blocks) -> None:
        nonlocal n, b
        if not isinstance(blocks, list):
            return
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") == "image":
                src = blk.get("source")
                data = src.get("data") if isinstance(src, dict) else None
                if isinstance(data, str):  # a URL image carries no base64 bytes to discount
                    n += 1
                    b += len(data)
            elif blk.get("type") == "tool_result":
                walk(blk.get("content"))

    msgs = req.get("messages") if isinstance(req, dict) else None
    if isinstance(msgs, list):
        for m in msgs:
            if isinstance(m, dict):
                walk(m.get("content"))
    return b, n


def body_size(req, raw_len: int) -> int:
    """A request's size for the estimate: its byte length with each base64 image counted at a flat
    IMAGE_COST_BYTES instead of its raw bytes (a 600 KB screenshot is a few thousand tokens, not 150K).
    Pure, never raises; the caller walks it only when the raw body holds "image" (the cheap test first)."""
    b, n = _image_bytes(req)
    return raw_len - b + n * IMAGE_COST_BYTES


class Anchors:
    """Per-conversation size anchors: the sent model, the request body size and the input total of
    the last served response, so the next estimate is the anchor plus the growth. The key is the
    conversation (session, agent) — `drop` forgets a model's anchor with the rest, while `estimate`
    ignores one noted for another model: token figures don't carry across models. Request threads
    share one instance; a lock guards the map."""

    def __init__(self):
        self._lock = threading.Lock()
        self._map = BoundedDict(4096)

    def note(self, key, model, body_len: int, total: int, grow_only: bool = False) -> None:
        """Remember (model, body_len, total) for this conversation. A total of 0 or less is never an
        anchor (zeroed usage would disable the check for the session); `grow_only` (a count_tokens
        answer) never shrinks an existing anchor: it replaces one only when its body is at least as long."""
        if total <= 0:
            return
        with self._lock:
            old = self._map.get(key)
            if old is not None and grow_only and body_len < old[1]:
                return
            self._map.pop(key, None)  # re-insert as newest: a hot key is not evicted by subagent churn
            self._map[key] = (model, body_len, total)

    def drop(self, key) -> None:
        with self._lock:
            self._map.pop(key, None)

    def estimate(self, key, model, body_len: int) -> int:
        """Estimated tokens for a request of this size on this model: the anchor's input total plus
        the body's growth over the anchor (bytes/4), or bytes/4 with no anchor (or one noted for
        another model). A body that shrank is never estimated below the anchor's figure."""
        with self._lock:
            anchor = self._map.get(key)
        if anchor is None or anchor[0] != model:
            return body_len // 4
        _, anchor_len, total = anchor
        return total + max(0, body_len - anchor_len) // 4


class Refusals:
    """The compact loop breaker: the compaction exemption hangs on one undocumented header, and a
    Claude Code that stops sending it would have its compaction request refused too, so the session
    could never compact. Never twice in a row for one conversation key: the second hit within
    REFUSE_S seconds goes to the downstream and its own error decides. Request threads share one
    instance; a lock guards the map."""

    # ponytail: 60 s of let-through per conversation, fixed — enough for a compaction request and
    # its retry, no way around the check by waiting.
    REFUSE_S = 60

    def __init__(self):
        self._lock = threading.Lock()
        self._map = BoundedDict(4096)

    def refuse(self, key) -> bool:
        """True to refuse this one (and remember when it was refused); False — clearing the memory —
        when the key was refused within the last REFUSE_S seconds: let it through."""
        with self._lock:
            t = self._map.get(key)
            if t is not None and time.time() - t < self.REFUSE_S:
                self._map.pop(key, None)
                return False
            self._map.pop(key, None)
            self._map[key] = time.time()
            return True

    def clear(self, key) -> None:
        """A served (non-refused) request for the key: any streak is over."""
        with self._lock:
            self._map.pop(key, None)


def over(estimate: int, window: int, max_tokens: int = 0):
    """The token limit when the estimate is over it, else None: the smaller of LIMIT of the window and
    the window minus the request's max_tokens (prompt plus output both count against it), never below
    half the window."""
    limit = max(min(int(window * LIMIT), window - max_tokens), window // 2)
    return limit if estimate >= limit else None


def error_body(estimate: int, limit: int) -> bytes:
    """The refusal Claude Code compacts on — the same shape Anthropic answers with."""
    return json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                      "message": f"prompt is too long: {estimate} tokens > {limit} maximum"}}).encode()
