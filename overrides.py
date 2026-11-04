"""Settings overrides: console.json lines that set a request's window or model when a condition holds.
Pure. The proxy applies them in prepare(); the console edits them through the API (settings_from_json validates)."""
import json
from dataclasses import dataclass, replace

from rulelang import RuleError, evaluate, parse_cond
from routing import MODEL_TIERS, WINDOWS, tier_of


@dataclass(frozen=True)
class Setting:
    when: str        # the condition's text as typed, blank for "always"
    cond: object     # parse_cond(when), None when blank
    downstream: str
    tier: str | None  # a MODEL_TIERS entry or None for every tier
    key: str         # "window" | "model"
    value: str


def _fields(c):
    return {c[1]} if c[0] == "cmp" else _fields(c[1]) | (_fields(c[2]) if len(c) > 2 else set())


def _session_ids(c):
    """The session ids when the condition is only `session is X` terms joined by or, else None."""
    if c is None:
        return None
    ids = set()

    def walk(c):
        if c[0] == "or":
            return walk(c[1]) and walk(c[2])
        if c[0] == "cmp" and c[1] == "session" and c[2] == "is":
            ids.add(c[3])
            return True
        return False

    return frozenset(ids) if walk(c) else None


def is_session_line(s):
    """The Setting's session ids when its condition is only `session is X` terms joined by or, else None."""
    return _session_ids(s.cond)


def settings_from_json(items, downstreams):
    """console.json's settings list -> Settings; ValueError("settings line N: …") on bad input."""
    if not isinstance(items, list):
        raise ValueError("settings must be a list")
    kinds = {d.id: d.kind for d in downstreams}
    out = []
    for n, it in enumerate(items, 1):
        def bad(msg):
            raise ValueError(f"settings line {n}: {msg}")
        if not isinstance(it, dict):
            bad("must be an object")
        when, ds, tier = it.get("when", ""), it.get("downstream"), it.get("tier")
        key, value = it.get("set"), it.get("value")
        if not isinstance(when, str):
            bad("when must be text")
        if ds not in kinds:
            bad(f"no downstream named {ds!r}")
        if key not in ("window", "model"):
            bad("set must be window or model")
        if tier is not None and (not isinstance(tier, str) or tier not in MODEL_TIERS):
            bad("tier must be one of " + ", ".join(MODEL_TIERS) + " or null")
        if not isinstance(value, str) or not value:
            bad("value must be non-empty text")
        if key == "window":
            if kinds[ds] != "valar":
                bad("a window setting needs a valar downstream")
            if value not in WINDOWS:
                bad("window must be " + ", ".join(WINDOWS))
        try:
            cond = parse_cond(when)
        except RuleError as e:
            bad(str(e))
        if cond is not None and "label" in _fields(cond):
            bad("label can't be used in a setting")
        out.append(Setting(when, cond, ds, tier, key, value))
    return tuple(out)


def settings_to_json(settings):
    return [{"when": s.when, "downstream": s.downstream, "tier": s.tier, "set": s.key, "value": s.value}
            for s in settings]


def slot(model):
    """The tier a request's model uses, mirroring track_window: non-claude (and unmatched claude) models use sonnet's."""
    t = tier_of(model) if model.startswith("claude-") else "sonnet"
    return t if t in MODEL_TIERS else "sonnet"


def resolve(settings, ds_id, model, get):
    """First matching line per key: {"window": (value, Setting), "model": (value, Setting)}; absent keys stay unset."""
    s, out = slot(model), {}
    for st in settings:
        if st.downstream != ds_id or st.key in out:
            continue
        if st.key == "model" and not model.startswith("claude-"):
            continue  # matching track_model's passthrough: a model override only ever applies to claude-* models
        if st.tier is not None and st.tier != s:
            continue
        if st.cond is not None and not evaluate(st.cond, get):
            continue
        out[st.key] = (st.value, st)
    return out


def _hour_only(c):
    return c is not None and _fields(c) == {"hour"}


def _hset(c):
    return frozenset(h for h in range(24) if evaluate(c, lambda n: h))


def _ranges(hours):
    out, start, prev = [], None, None
    for h in sorted(hours):
        if start is not None and h == prev + 1:
            prev = h
        else:
            if start is not None:
                out.append((start, prev))
            start, prev = h, h
    if start is not None:
        out.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in out)


def collisions(settings):
    """Lines that can never fire because an earlier line already matched: {"line": j, "by": i, "text": str}."""
    out = []
    for j, b in enumerate(settings):
        for i, a in enumerate(settings[:j]):
            if a.downstream != b.downstream or a.key != b.key or a.value == b.value:
                continue
            if not (a.tier is None or b.tier is None or a.tier == b.tier):
                continue
            if _hour_only(a.cond) and _hour_only(b.cond):
                shared = _hset(a.cond) & _hset(b.cond)
                if not shared:
                    continue
                when = " at hours " + _ranges(shared)
            elif _session_ids(a.cond) is not None and _session_ids(b.cond) is not None \
                    and not (_session_ids(a.cond) & _session_ids(b.cond)):
                continue
            else:
                when = ""
            out.append({"line": j + 1, "by": i + 1, "text": f"line {j + 1} is shadowed by line {i + 1}{when}"})
    return out


def _rebuild(st, ids):
    when = " or ".join(f"session is {i}" for i in sorted(ids))
    return replace(st, when=when, cond=parse_cond(when))


def prune(settings, last_seen, now, days=7):
    """Drop ids last seen more than `days` ago from session lines; a line left with no ids goes."""
    out = []
    for st in settings:
        ids = _session_ids(st.cond)
        if ids is None:
            out.append(st)
            continue
        # ponytail: an id with unknown last_seen is kept; guessing needs the store, which prune stays clear of
        keep = frozenset(i for i in ids if last_seen(i) is None or now - last_seen(i) <= days * 86400)
        if keep:
            out.append(_rebuild(st, keep) if keep != ids else st)
    return tuple(out)


def upsert_session(settings, sid, ds_id, tier, key, value):
    """Set one session's value: replace the `session is <sid>` line in place, or insert it at the top. None removes."""
    sid = sid.lower()

    def match(st):
        return st.downstream == ds_id and st.tier == tier and st.key == key and _session_ids(st.cond) == frozenset((sid,))

    if value is None:
        return tuple(st for st in settings if not match(st))
    hit = next((n for n, st in enumerate(settings) if match(st)), None)
    if hit is None:
        when = f"session is {sid}"
        return (Setting(when, parse_cond(when), ds_id, tier, key, value),) + tuple(settings)
    return tuple(settings[:hit]) + (replace(settings[hit], value=value),) + tuple(settings[hit + 1:])


def reset_session(settings, sid, ds_id):
    """Take the sid out of every session line for ds_id; a line left with no ids goes."""
    sid = sid.lower()
    out = []
    for st in settings:
        ids = _session_ids(st.cond)
        if ids is None or st.downstream != ds_id or sid not in ids:
            out.append(st)
            continue
        keep = ids - frozenset((sid,))
        if keep:
            out.append(_rebuild(st, keep))
    return tuple(out)


def describe(applied):
    """What resolve() picked, as JSON for the row's override column; None when nothing applied."""
    if not applied:
        return None
    return json.dumps([{"set": k, "value": applied[k][0], "when": applied[k][1].when}
                       for k in ("window", "model") if k in applied])
