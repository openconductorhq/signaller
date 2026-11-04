"""Prices: what a request cost on the track that served it, and what it would have cost on Anthropic's API."""
import json
import os
import sys

from routing import PRICE_KEYS as KEYS, tier_of

VALAR_TIERS = {"realtime": "asap", "priority": "priority", "standard": "standard"}  # flex/batch: never sent
_SONNET = {"in": 2.0, "out": 10.0, "cache_read": 0.20, "cache_write": 2.5, "cache_write_1h": 4.0}
# USD per 1M tokens. Cache write = 1.25 x input (5-minute entries), 2 x input (1-hour entries).
# ponytail: Haiku's higher rate for >100K-token prompts is ignored; api_cost is an estimate.
DEFAULT_ANTHROPIC = {
    "fable": {"in": 10.0, "out": 50.0, "cache_read": 0.25, "cache_write": 12.5, "cache_write_1h": 20.0},
    "opus": {"in": 4.0, "out": 20.0, "cache_read": 0.20, "cache_write": 5.0, "cache_write_1h": 8.0},
    "sonnet": _SONNET,
    "haiku": {"in": 0.10, "out": 0.50, "cache_read": 0.01, "cache_write": 0.125, "cache_write_1h": 0.2},
    "other": dict(_SONNET),
}


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
        self.dir, self.listed, self.anthropic = config_dir, {}, DEFAULT_ANTHROPIC
        self.load()

    def load(self) -> None:
        try:
            with open(os.path.join(self.dir, "models.json")) as f:
                saved = json.load(f)
            self.listed = {**({"valar": saved["valar_prices"]} if saved.get("valar_prices") else {}),
                           **(saved.get("listed_prices") or {})}  # valar_prices: the pre-downstreams key
        except (OSError, ValueError, AttributeError, TypeError):
            self.listed = {}
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

    def set_listed(self, id: str, prices: dict) -> None:
        self.listed = {**self.listed, id: prices}  # one reference swap; readers see old or new

    def cost(self, ds, model_out: str, window: str | None, usage: dict) -> float | None:
        """What the downstream charged (None = unknown); ds None is Anthropic, the subscription."""
        if all(usage.get(k) is None for k in KEYS):
            return None
        if ds is None:
            return 0.0
        u = [usage.get(k) or 0 for k in KEYS]
        if ds.kind == "valar":
            p = self.listed.get(ds.id, {}).get(model_out, {}).get(window)
            return (u[0] * p[0] + u[2] * p[1] + u[1] * p[2]) / 1e6 if p else None  # Valar cache writes are free
        # ponytail: generic downstreams price 1h writes at their one cache_write rate
        return sum(x * ds.prices[k] for x, k in zip(u, KEYS)) / 1e6 if ds.prices else None

    def api_cost(self, model_in: str, usage: dict) -> float | None:
        if all(usage.get(k) is None for k in KEYS):
            return None
        row = self.anthropic.get(tier_of(model_in)) or self.anthropic["sonnet"]
        w = usage.get("cache_write") or 0
        h = min(usage.get("cache_write_1h") or 0, w)
        return ((usage.get("in") or 0) * row["in"] + (usage.get("out") or 0) * row["out"]
                + (usage.get("cache_read") or 0) * row["cache_read"]
                + (w - h) * row["cache_write"] + h * row.get("cache_write_1h", 2 * row["in"])) / 1e6
