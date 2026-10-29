"""Prices: what a request cost on the track that served it, and what it would have cost on Anthropic's API."""
import json
import os
import sys

from routing import tier_of

VALAR_TIERS = {"realtime": "asap", "priority": "priority", "standard": "standard"}  # flex/batch: never sent
_SONNET = {"in": 2.0, "out": 10.0, "cache_read": 0.20, "cache_write": 2.5}
# USD per 1M tokens. Cache write = 1.25 x input.
# ponytail: Haiku's higher rate for >100K-token prompts is ignored; api_cost is an estimate.
DEFAULT_ANTHROPIC = {
    "fable": {"in": 10.0, "out": 50.0, "cache_read": 0.25, "cache_write": 12.5},
    "opus": {"in": 4.0, "out": 20.0, "cache_read": 0.20, "cache_write": 5.0},
    "sonnet": _SONNET,
    "haiku": {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "other": dict(_SONNET),
}
KEYS = ("in", "out", "cache_read", "cache_write")


def parse_valar(payload: dict) -> dict:
    """{model_id: {window: [in, cached_in, out]}} per 1M tokens, from Valar's /v1/models; unpriced models omitted."""
    out = {}
    for m in payload.get("data") or []:
        p = {VALAR_TIERS[t["tier"]]: [float(t["input_per_1m_tokens"]), float(t["cached_input_per_1m_tokens"]),
                                      float(t["output_per_1m_tokens"])]
             for t in m.get("pricing") or [] if t.get("tier") in VALAR_TIERS}
        if p:
            out[m["id"]] = p
    return out


class Prices:
    def __init__(self, config_dir: str):
        self.dir, self.valar, self.anthropic = config_dir, {}, DEFAULT_ANTHROPIC
        self.load()

    def load(self) -> None:
        try:
            with open(os.path.join(self.dir, "models.json")) as f:
                self.valar = json.load(f).get("valar_prices") or {}
        except (OSError, ValueError, AttributeError):
            self.valar = {}
        path = os.path.join(self.dir, "prices.json")
        try:
            with open(path) as f:
                got = json.load(f)
            self.anthropic = {t: {**row, **(got.get(t) or {})} for t, row in DEFAULT_ANTHROPIC.items()}
        except FileNotFoundError:
            self.anthropic = DEFAULT_ANTHROPIC
            try:
                with open(path, "w") as f:
                    json.dump(DEFAULT_ANTHROPIC, f, indent=1)
            except OSError as e:
                print("prices.json not written: %s" % e, file=sys.stderr, flush=True)
        except (OSError, ValueError, AttributeError, TypeError) as e:
            self.anthropic = DEFAULT_ANTHROPIC
            print("prices.json unreadable, default prices in use: %s" % e, file=sys.stderr, flush=True)

    def set_valar(self, prices: dict) -> None:
        self.valar = prices  # one reference swap; readers see old or new

    def cost(self, track: str, model_out: str, window: str | None, usage: dict) -> float | None:
        if all(usage.get(k) is None for k in KEYS):
            return None
        if track != "valar":
            return 0.0  # Anthropic: the subscription; local: our own hardware
        p = self.valar.get(model_out, {}).get(window)
        if not p:
            return None
        u = [usage.get(k) or 0 for k in KEYS]
        return (u[0] * p[0] + u[2] * p[1] + u[1] * p[2]) / 1e6  # Valar cache writes are free

    def api_cost(self, model_in: str, usage: dict) -> float | None:
        if all(usage.get(k) is None for k in KEYS):
            return None
        row = self.anthropic.get(tier_of(model_in)) or self.anthropic["sonnet"]
        return sum((usage.get(k) or 0) * row[k] for k in KEYS) / 1e6
