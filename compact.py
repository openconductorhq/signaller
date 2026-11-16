"""Windows, size estimate and refusal body for compacting before a downstream model's window.

Pure helpers: no I/O, no state beyond `Anchors`' in-memory map. Later tasks wire
these into the request path; nothing here knows about requests.
"""

import json
import threading

from autophase import BoundedDict

# ponytail: the compaction trigger is fixed at 90% of the window, not configurable (a design decision).
LIMIT = 0.9

WINDOW_KEYS = ("context_length", "max_input_tokens", "max_model_len")

FLOOR = 1000  # a window below this is nonsense (a broken list or a hand-edit): treated as not listed
CEILING = 10_000_000  # and above this: the manual-window range routing validates a config against


def _window_int(v) -> bool:
    """A value that may act as a context window: a positive int at least FLOOR (manual windows are validated 1000..10_000_000 at config time)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= FLOOR


def _entries(payload: dict):
    """A models payload's `data` entries that are dicts: a malformed payload or entry yields nothing."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                yield entry


def listed_windows(payload: dict) -> dict:
    """Windows from a downstream's `/v1/models` payload: per model id, the first
    int > 0 of `context_length`, `max_input_tokens`, `max_model_len`. Models with
    none are omitted. Malformed entries are skipped, never raised."""
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
    `model_info.max_input_tokens` (int > 0), the smallest when a name repeats."""
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
    # a hand-edited models.json can give a listed value of any kind: only a positive int is a window
    v = listed.get(ds.id, {}).get(model) if isinstance(listed, dict) else None
    return v if _window_int(v) else None


class Anchors:
    """Per-conversation size anchors: the request body length and input total of
    the last served response, so the next estimate is the anchor plus the growth.
    Request threads share one instance; a lock guards the map."""

    def __init__(self):
        self._lock = threading.Lock()
        self._map = BoundedDict(4096)

    def note(self, key, body_len: int, total: int) -> None:
        with self._lock:
            self._map.pop(key, None)  # re-insert as newest: a hot key is not evicted by subagent churn
            self._map[key] = (body_len, total)

    def drop(self, key) -> None:
        with self._lock:
            self._map.pop(key, None)

    def estimate(self, key, body_len: int) -> int:
        """Estimated tokens for a request of this length: the anchor's input total
        plus the body's growth over the anchor (bytes/4), or bytes/4 with no anchor.
        A body that shrank is never estimated below the anchor's figure."""
        with self._lock:
            anchor = self._map.get(key)
        if anchor is None:
            return body_len // 4
        anchor_len, total = anchor
        return total + max(0, body_len - anchor_len) // 4


def over(estimate: int, window: int):
    """The token limit when the estimate is over it, else None."""
    limit = int(window * LIMIT)
    return limit if estimate >= limit else None


def error_body(estimate: int, limit: int) -> bytes:
    """The refusal Claude Code compacts on — the same shape Anthropic answers with."""
    return json.dumps({"type": "error", "error": {"type": "invalid_request_error",
                      "message": f"prompt is too long: {estimate} tokens > {limit} maximum"}}).encode()
