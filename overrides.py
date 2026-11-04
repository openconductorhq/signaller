"""Settings overrides: console.json lines that set a request's window or model when a condition holds.
Pure. The proxy applies them in prepare(); the console edits them through the API (settings_from_json validates)."""
import json
from dataclasses import dataclass, replace

from rulelang import RuleError, evaluate, fields_of, parse_cond
from routing import MODEL_TIERS, WINDOWS, slot


@dataclass(frozen=True)
class Setting:
    when: str        # the condition's text as typed, blank for "always"
    cond: object     # parse_cond(when), None when blank
    downstream: str
    tier: str | None  # a MODEL_TIERS entry or None for every tier
    key: str         # "window" | "model"
    value: str


def session_ids(st):
    """The Setting's session ids when its condition is only `session is X` terms joined by or, else None."""
    c = st.cond
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


def setting_from_json(it, downstreams, n):
    """One settings line (console.json's shape) -> a Setting; ValueError("settings line N: …") on bad input."""
    kinds = {d.id: d.kind for d in downstreams}

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
    if cond is not None and "label" in fields_of(cond):
        bad("label can't be used in a setting")
    return Setting(when, cond, ds, tier, key, value)


def settings_from_json(items, downstreams):
    """console.json's settings list -> Settings; ValueError("settings line N: …") on bad input."""
    if not isinstance(items, list):
        raise ValueError("settings must be a list")
    return tuple(setting_from_json(it, downstreams, n) for n, it in enumerate(items, 1))


def settings_to_json(settings):
    return [{"when": s.when, "downstream": s.downstream, "tier": s.tier, "set": s.key, "value": s.value}
            for s in settings]


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
    return c is not None and fields_of(c) == {"hour"}


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
    ids = [session_ids(st) for st in settings]
    hours = [_hset(st.cond) if _hour_only(st.cond) else None for st in settings]
    out = []
    for j, b in enumerate(settings):
        for i, a in enumerate(settings[:j]):
            if a.downstream != b.downstream or a.key != b.key or a.value == b.value:
                continue
            if not (a.tier is None or b.tier is None or a.tier == b.tier):
                continue
            if hours[i] is not None and hours[j] is not None:
                shared = hours[i] & hours[j]
                if not shared:
                    continue
                when = " at hours " + _ranges(shared)
            elif ids[i] is not None and ids[j] is not None and not (ids[i] & ids[j]):
                continue
            else:
                when = ""
            out.append({"line": j + 1, "by": i + 1, "text": f"line {j + 1} is shadowed by line {i + 1}{when}"})
    return out


def _rebuild(st, ids):
    when = " or ".join(f"session is {i}" for i in sorted(ids))
    return replace(st, when=when, cond=parse_cond(when))


def _drop_ids(settings, gone):
    """A line with its ids filtered out (gone(st, id) → True removes that id); a line left with no ids goes."""
    out = []
    for st in settings:
        ids = session_ids(st)
        if ids is None:
            out.append(st)
            continue
        keep = frozenset(i for i in ids if not gone(st, i))
        if keep:
            out.append(_rebuild(st, keep) if keep != ids else st)
    return tuple(out)


def prune(settings, last_seen, now, days=7):
    """Drop ids last seen more than `days` ago from session lines; a line left with no ids goes."""
    # ponytail: an id with unknown last_seen is kept; guessing needs the store, which prune stays clear of
    return _drop_ids(settings, lambda st, i: last_seen(i) is not None and now - last_seen(i) > days * 86400)


def upsert_session(settings, sid, ds_id, tier, key, value):
    """Set one session's value: replace the `session is <sid>` line in place, or insert it at the top. None removes."""
    sid = sid.lower()

    def match(st):
        return st.downstream == ds_id and st.tier == tier and st.key == key and session_ids(st) == frozenset((sid,))

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
    return _drop_ids(settings, lambda st, i: st.downstream == ds_id and i == sid)


def describe(applied):
    """What resolve() picked, as JSON for the row's override column; None when nothing applied."""
    if not applied:
        return None
    return json.dumps([{"set": k, "value": applied[k][0], "when": applied[k][1].when}
                       for k in ("window", "model") if k in applied])
