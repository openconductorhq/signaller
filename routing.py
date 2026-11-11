"""Routing engine: features of a request, user-editable rules, and the decision with its explanation."""
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Callable

from autophase import AutoSettings, human_index, human_text, text_hash, turn_text
from rulelang import FIELDS, KEYWORDS, RuleError, evaluate, fields_of, parse

ROUTED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
TAG_RE = re.compile(r"(?<!\w)#(\w+)", re.I)
# tags count only at the very start of the prompt ("#execution #local do x"), so a "#planning" mid-sentence or in a
# pasted log can't hijack the turn. ponytail: a tag starts with a letter, so "#123 broke" is left alone
LEAD_TAGS_RE = re.compile(r"\A\s*(?:#[A-Za-z]\w*[:,]?(?:\s+|\Z))+")
TEXT_CAP = 20_000
MAX_DOWNSTREAMS = 6
LEVER_MODES = ("rules", "auto")  # the levers that aren't a track: every listing of them uses this
ID_RE = re.compile(r"[a-z][a-z0-9-]{0,23}")
COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")
RESERVED_IDS = {"anthropic", "reset", "name", *LEVER_MODES} | KEYWORDS | set(FIELDS)
PRICE_KEYS = ("in", "out", "cache_read", "cache_write")


def latest_human_text(messages: list) -> str:
    """Text of the most recent user message that has non-system-reminder text."""
    i = human_index(messages)
    return human_text(messages[i]) if i >= 0 else ""


def lead_tags(text: str) -> frozenset:
    m = LEAD_TAGS_RE.match(text)
    return frozenset(t.lower() for t in TAG_RE.findall(m.group())) if m else frozenset()


def untagged(text: str) -> str:
    """The prompt without its leading #tags, as upstreams get it; a prompt that is only tags stays as is."""
    m = LEAD_TAGS_RE.match(text)
    return text[m.end():] if m and text[m.end():].strip() else text


def strip_lead_tags(req: dict) -> bool:
    """Drop the leading #tags from every user prompt in the request (upstreams don't need them).
    True when anything changed. A prompt that is only tags is kept as is, never sent empty."""
    hit = False
    for msg in req.get("messages") or []:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        c = msg.get("content")
        if isinstance(c, str):
            holder, key = msg, "content"
        elif isinstance(c, list):
            holder, key = next((b for b in c if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
                                and not b["text"].strip().startswith("<system-reminder>")), None), "text"
            if holder is None:
                continue
        else:
            continue
        t = untagged(holder[key])
        if t != holder[key]:
            holder[key], hit = t, True
    return hit


STEP_CAP = 600  # stored per request; ponytail: a snippet of each result, not the whole output


def latest_tool_results(messages: list) -> str | None:
    """What a tool-loop step sends back: "Bash: <result>" per tool_result in the last user message; None otherwise."""
    if not messages or messages[-1].get("role") != "user" or not isinstance(messages[-1].get("content"), list):
        return None
    prev = messages[-2].get("content") if len(messages) > 1 and messages[-2].get("role") == "assistant" else None
    names = {b.get("id"): b.get("name") for b in prev if isinstance(b, dict) and b.get("type") == "tool_use"} \
        if isinstance(prev, list) else {}
    out = []
    for b in messages[-1]["content"]:
        if not isinstance(b, dict) or b.get("type") != "tool_result":
            continue
        c = b.get("content")
        c = c if isinstance(c, list) else [{"type": "text", "text": c if isinstance(c, str) else ""}]
        text = " ".join(x.get("text", "") if x.get("type") == "text" else "[%s]" % x.get("type")
                        for x in c if isinstance(x, dict))
        out.append("%s%s: %s" % (names.get(b.get("tool_use_id")) or "tool", " (error)" if b.get("is_error") else "",
                                 text.strip() or "(empty)"))
    return "\n".join(out)[:STEP_CAP] if out else None


def has_images(req) -> bool:
    """Any image in the conversation, typed by the person or returned by a tool. Claude Code resends history, so once
    one is in, every later request of the session carries it."""
    def any_image(blocks):
        return isinstance(blocks, list) and any(
            isinstance(b, dict) and (b.get("type") == "image" or (b.get("type") == "tool_result" and any_image(b.get("content"))))
            for b in blocks)
    msgs = req.get("messages") if isinstance(req, dict) else None
    return isinstance(msgs, list) and any(isinstance(m, dict) and any_image(m.get("content")) for m in msgs)


def _sid_ok(sid) -> bool:
    """The id becomes a filename and a filter token: plain slug only, and not the null bucket's name."""
    return isinstance(sid, str) and sid != "unknown" and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid) is not None


def session_of(headers, req) -> str | None:
    """Claude Code session id: the header, else metadata.user_id's JSON. Never raises."""
    try:
        sid = headers.get("X-Claude-Code-Session-Id")  # http.client headers are case-insensitive
        if not sid:
            sid = {k.lower(): v for k, v in headers.items()}.get("x-claude-code-session-id")
        if _sid_ok(sid):
            return sid
        sid = json.loads(req["metadata"]["user_id"])["session_id"]
        return sid if _sid_ok(sid) else None
    except Exception:
        return None


def is_routed(method: str, path: str) -> bool:
    return method == "POST" and path.split("?", 1)[0] in ROUTED_PATHS


def is_advisor(t: dict) -> bool:
    """Claude Code's Advisor tool definition (type advisor_20260301): run by Anthropic, unknown to a downstream."""
    return str(t.get("type") or "").startswith("advisor_")


def tier_of(model: str) -> str:
    m = model.lower()
    return next((t for t in ("haiku", "sonnet", "opus", "fable") if t in m), "other")


MODEL_TIERS = ("fable", "opus", "sonnet", "haiku")


WINDOWS = ("asap", "priority", "standard")  # Valar completion windows; flex rejects streaming, batch is async: never sent


def slot(model: str) -> str:
    """The tier a request's model uses, mirroring track_window: non-claude (and unmatched claude) models use sonnet's."""
    t = tier_of(model) if model.startswith("claude-") else "sonnet"
    return t if t in MODEL_TIERS else "sonnet"


@dataclass(frozen=True)
class Downstream:
    id: str
    name: str
    url: str
    kind: str  # "valar" | "generic"
    models: dict  # tier -> model id, every MODEL_TIERS
    color: str
    windows: dict | None = None  # tier -> WINDOWS value; valar only
    prices: dict | None = None  # PRICE_KEYS -> USD per 1M tokens; generic only, None = unpriced
    advisor: bool = True  # Claude Code's Advisor tool works on this downstream


def track_model(model: str, ds: Downstream) -> str:
    """The model id a downstream serves for a request's model; non-claude ids pass through."""
    if not model.startswith("claude-"):
        return model
    return ds.models.get(slot(model), ds.models["sonnet"])


def track_window(model: str, ds: Downstream) -> str | None:
    """The Valar completion window for a request's model (same slot lookup as track_model); None off Valar."""
    if ds.kind != "valar":
        return None
    w = ds.windows
    return w.get(slot(model), w["sonnet"])


class Features:
    def __init__(self, text, tags, model, tokens, subagent, turn_key, servertool=False, step=None, session=None, hour=None,
                 teammate=False, messages=None, tools=False):
        self.messages = messages if messages is not None else []
        self.text, self.tags, self.model, self.tokens, self.servertool = text[:TEXT_CAP], tags, model, tokens, servertool
        self.step = step  # the tool results this request carries back (display only; routing uses text)
        self.prompt = untagged(text)  # uncapped and as sent upstream (no leading tags), for display
        self.tier, self.subagent, self.turn_key = tier_of(model), subagent, turn_key
        self.session, self.hour, self.teammate, self.tools = session, hour, teammate, tools

    @classmethod
    def from_body(cls, body: bytes, req=None):
        """None when the body isn't a messages request we can route (passthrough). `req`: the body already parsed."""
        try:
            req = json.loads(body) if req is None else req
            model = req["model"]
            if not isinstance(model, str):
                return None
            text = latest_human_text(req["messages"])
            sysm = req.get("system")
            first = sysm[0] if isinstance(sysm, list) and sysm else sysm
            first = first.get("text", "") if isinstance(first, dict) else first
            sub = isinstance(first, str) and "cc_is_subagent=true" in first
            # a typed tool (web_search_20250305, web_fetch_…, code_execution_…) runs on Anthropic's side;
            # other upstreams 400 on it. Claude Code sends these in their own small requests (WebSearch).
            # The Advisor (advisor_…) rides on every main-loop request since Claude Code 2.1.3xx: it doesn't pin the
            # turn to Anthropic; prepare() drops it on the way to a downstream (strip_advisor).
            srv = any(isinstance(t, dict) and t.get("type") not in (None, "custom") and not is_advisor(t)
                      for t in req.get("tools") or [])
            # Claude Code's own check of the whole conversation (auto-mode permissions): keep it on Anthropic too
            srv = srv or text.startswith("<transcript>")
            step = latest_tool_results(req["messages"])
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return None
        return cls(text, lead_tags(text), model, len(body) // 4, sub, text_hash(text), srv, step,
                   messages=req["messages"], tools=bool(req.get("tools")))

    def get(self, name: str, label: Callable[[], str]):
        return label() if name == "label" else getattr(self, {"tag": "tags"}.get(name, name))


@dataclass(frozen=True)
class ClassResult:
    label: str | None
    confidence: float | None
    note: str


@dataclass(frozen=True)
class Label:
    name: str
    description: str
    track: str


@dataclass(frozen=True)
class ClassifierSettings:
    model: str
    threshold: float
    timeout_s: float
    labels: tuple[Label, ...]
    upstream: str = "valar"


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str  # "dsl" | "classifier"
    enabled: bool
    text: str
    builtin: str | None = None  # "servertool" | "tags" | "classifier" | "tier" | None
    lines: tuple = field(init=False, compare=True, default=())

    def __post_init__(self):
        if self.kind == "dsl":
            try:
                object.__setattr__(self, "lines", tuple(parse(self.text)))
            except RuleError as e:
                raise ValueError(f"rule '{self.name}': {e}") from None


@dataclass(frozen=True)
class Doubling:
    rate: float
    tag: str
    shadow: dict  # served track -> track that answers the same turn too, or None


@dataclass(frozen=True)
class RoutingConfig:
    lever: str
    otherwise: str
    rules: tuple[Rule, ...]
    classifier: ClassifierSettings
    doubling: Doubling
    downstreams: tuple[Downstream, ...]
    seeded: bool = False  # the valar downstream got its url/key from the env once
    migrated_off: tuple[str, ...] = field(default=(), compare=False)  # rules the v2→v3 migration switched off; banner only
    settings: tuple = ()  # overrides.Setting lines; last so positional constructors stay valid
    prompts: tuple = ()  # (name, text): what /signaller run <name> submits in Claude Code
    auto: AutoSettings = AutoSettings()


def tracks(cfg: RoutingConfig) -> tuple[str, ...]:
    return ("anthropic",) + tuple(d.id for d in cfg.downstreams)


def exec_track(cfg: RoutingConfig) -> str:
    """Where auto sends execution: auto.execution if it names a downstream, else the first downstream, else Anthropic."""
    if cfg.auto.execution and downstream(cfg, cfg.auto.execution):
        return cfg.auto.execution
    return cfg.downstreams[0].id if cfg.downstreams else "anthropic"


def downstream(cfg: RoutingConfig, id: str) -> Downstream | None:
    return next((d for d in cfg.downstreams if d.id == id), None)


def valid_id(id: str, taken: Iterable[str]) -> str | None:
    """Why id can't name a new downstream, or None."""
    if not isinstance(id, str) or not ID_RE.fullmatch(id):
        return "id must be a lowercase letter, then up to 23 lowercase letters, digits or dashes"
    if id in RESERVED_IDS:
        return f"'{id}' is reserved"
    if id in taken:
        return f"there is already a downstream named {id}"
    return None


def references(cfg: RoutingConfig, id: str, session_levers: Mapping[str, str]) -> list[str]:
    """What still points at a track, in words; any of these blocks deleting it. Switched-off rules don't count."""
    out = ["the lever"] if cfg.lever == id else []
    out += [f"session {sid[:8]}'s lever" for sid, lv in session_levers.items() if lv == id]
    out += [f"rule '{r.name}' line {n}" for r in cfg.rules if r.enabled for n, ln in enumerate(r.lines, 1) if ln.track == id]
    out += [f"settings line {n}" for n, s in enumerate(cfg.settings, 1) if s.downstream == id]
    out += ["Otherwise"] if cfg.otherwise == id else []
    out += ["auto execution"] if cfg.auto.execution == id else []
    out += [f"classifier label '{l.name}'" for l in cfg.classifier.labels if l.track == id]
    out += ["classifier upstream"] if cfg.classifier.upstream == id else []
    out += [f"side by side: {t} → {o}" for t, o in cfg.doubling.shadow.items() if o == id]  # its own shadow[id] goes with it
    return out


SERVERTOOL_RULE = Rule("Tools only Anthropic can run", "dsl", True, "when servertool go anthropic", "servertool")

DEFAULT_CONFIG = RoutingConfig(
    lever="rules", otherwise="anthropic",
    rules=(
        SERVERTOOL_RULE,
        Rule("Tags in your message", "dsl", True, "when tag is planning go anthropic\nwhen tag is execution go valar", "tags"),
        Rule("What the prompt is about", "classifier", True, "", "classifier"),
        Rule("Model the request asked for", "dsl", True,
             "when tier is opus or tier is fable go anthropic\n"
             "when tier is sonnet or tier is haiku or tier is other go valar", "tier"),
    ),
    classifier=ClassifierSettings("deepseek-ai/DeepSeek-V4.1-Flash", 0.7, 2.5, (
        Label("planning", "designing, specifying or planning work", "anthropic"),
        Label("design & review", "judging, reviewing or critiquing code or ideas", "anthropic"),
        Label("coding", "writing, editing, fixing or running code", "valar"),
        Label("chit-chat", "greetings, thanks, small talk", "valar"),
    ), "valar"),
    doubling=Doubling(0, "", {"anthropic": "valar", "valar": "anthropic"}),
    downstreams=(Downstream("valar", "Valar", "https://api.valarhq.ai", "valar",
                            {"fable": "zai-org/GLM-5.3", "opus": "zai-org/GLM-5.3", "sonnet": "zai-org/GLM-5.3",
                             "haiku": "deepseek-ai/DeepSeek-V4.1-Flash-1017"}, "#2F6DB5", {t: "asap" for t in MODEL_TIERS}),),
)
V2_TAGS_TEXT = "when tag is planning go anthropic\nwhen tag is local go local\nwhen tag is execution go valar"


def _num(v, lo, hi, msg):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:  # nan fails the range too
        raise ValueError(msg)
    return v


def _unit(v, what):
    return _num(v, 0, 1, f"{what} must be 0–1")


def _track(v, ts, what):
    if v not in ts:
        raise ValueError(f"{what}: no downstream named {v}")
    return v


def _v1_to_v2(obj):
    d = obj["doubling"]
    return {**obj, "version": 2, "models": {"valar": dict(DEFAULT_CONFIG.downstreams[0].models)},
            "classifier": {**obj["classifier"], "upstream": "local"},
            "doubling": {"rate": d["rate"], "tag": d["tag"],
                         "shadow": {"anthropic": "local" if d["when_anthropic"] else None, "valar": None,
                                    "local": "anthropic" if d["when_local"] else None}}}


def _goes_local(text):
    try:
        return any(ln.track == "local" for ln in parse(text))
    except RuleError:
        return False  # the load reports it


def _v2_to_v3(obj):
    """Valar becomes a downstream; Local is dropped and whatever pointed at it re-pointed (rules: switched off)."""
    loc = lambda v, to: to if v == "local" else v
    m = obj["models"]["valar"]  # configs from before fable/opus had their own model: those served sonnet's
    w = (obj.get("windows") or {}).get("valar", {})
    rules, off = [], []
    for r in obj["rules"]:
        if r.get("builtin") == "tags" and r.get("text") == V2_TAGS_TEXT:
            r = {**r, "text": DEFAULT_CONFIG.rules[1].text}
        elif r.get("kind") == "dsl" and r.get("enabled") and _goes_local(r.get("text", "")):
            r = {**r, "enabled": False}
            off.append(r["name"])
        rules.append(r)
    c, d = obj["classifier"], obj["doubling"]
    return {**obj, "version": 3, "seeded": False, "rules": rules,
            "lever": loc(obj["lever"], "rules"), "otherwise": loc(obj["otherwise"], "anthropic"),
            "classifier": {**c, "upstream": loc(c["upstream"], "valar"),
                           "labels": [{**l, "track": loc(l["track"], "anthropic")} for l in c["labels"]]},
            "doubling": {**d, "shadow": {t: loc(o, None) for t, o in d["shadow"].items() if t != "local"}},
            "downstreams": [{"id": "valar", "name": "Valar", "url": "https://api.valarhq.ai", "kind": "valar", "color": "#2F6DB5",
                             "models": {"fable": m["sonnet"], "opus": m["sonnet"], **{k: m[k] for k in m if k in MODEL_TIERS}},
                             "windows": {t: w.get(t, "asap") for t in MODEL_TIERS}, "prices": None}]}, tuple(off)


def _downstream(d, taken):
    err = valid_id(d["id"], taken)
    if err:
        raise ValueError(err)
    i = d["id"]
    if d["kind"] not in ("valar", "generic"):
        raise ValueError(f"downstream {i}: kind must be valar or generic")
    for k in ("name", "url", "color"):
        if not isinstance(d[k], str) or not d[k]:
            raise ValueError(f"downstream {i}: {k} must be non-empty text")
    if not COLOR_RE.fullmatch(d["color"]):
        raise ValueError(f"downstream {i}: color must be #RRGGBB")
    m = d["models"]
    if not isinstance(m, dict) or set(m) != set(MODEL_TIERS) or not all(isinstance(v, str) and v for v in m.values()):
        raise ValueError(f"downstream {i}: models must name a model for each of fable, opus, sonnet, haiku")
    windows = prices = None
    if d["kind"] == "valar":
        w = d.get("windows")
        if not isinstance(w, dict) or set(w) != set(MODEL_TIERS) or not all(isinstance(v, str) and v in WINDOWS for v in w.values()):
            raise ValueError(f"downstream {i}: windows must be asap, priority or standard for each of fable, opus, sonnet, haiku")
        windows = {t: w[t] for t in MODEL_TIERS}
    elif d.get("prices") is not None:
        p, msg = d["prices"], f"downstream {i}: prices must be in, out, cache_read and cache_write, each 0 or more"
        if not isinstance(p, dict) or set(p) != set(PRICE_KEYS):
            raise ValueError(msg)
        prices = {k: float(_num(p[k], 0, 1e6, msg)) for k in PRICE_KEYS}
    adv = d.get("advisor", True)
    if not isinstance(adv, bool):
        raise ValueError(f"downstream {i}: advisor must be true or false")
    return Downstream(i, d["name"], d["url"], d["kind"], {t: m[t] for t in MODEL_TIERS}, d["color"], windows, prices, adv)


PROMPT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


def prompts_from_json(items) -> tuple:
    """console.json's prompts -> ((name, text), ...); ValueError on bad input."""
    if not isinstance(items, list):
        raise ValueError("prompts must be a list")
    out = []
    for n, p in enumerate(items, 1):
        name, text = (p.get("name"), p.get("text")) if isinstance(p, dict) else (None, None)
        if not isinstance(name, str) or not PROMPT_NAME.fullmatch(name):
            raise ValueError(f"prompt {n}: name must be 1–32 of a-z, 0-9, - and _, starting with a letter or digit")
        if any(name == x for x, _ in out):
            raise ValueError(f"prompt {n}: another prompt is already named {name}")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000:
            raise ValueError(f"prompt {n}: text must be 1–20000 characters")
        out.append((name, text))
    return tuple(out)


def _auto_from_json(a, dss) -> AutoSettings:
    d = AutoSettings()
    if a is None:
        return d
    if not isinstance(a, dict):
        raise ValueError("auto: must be an object")

    def strs(k):
        v = a.get(k, getattr(d, k))
        if not isinstance(v, (list, tuple)) or len(v) > 20 or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in v):
            raise ValueError(f"auto {k}: up to 20 non-empty strings of at most 200 characters")
        return tuple(v)

    ex = a.get("execution")
    if ex is not None and ex not in [x.id for x in dss]:
        raise ValueError(f"auto execution: no downstream named {ex}")
    thr = _num(a.get("threshold", d.threshold), 0.5, 0.99, "auto threshold must be 0.5–0.99")
    cap = _num(a.get("train_cap", d.train_cap), 0.5, 1000, "auto train_cap must be 0.5–1000")
    return AutoSettings(ex, strs("planning_skills"), strs("execution_skills"), strs("plan_dirs"), float(thr), float(cap))


def config_from_json(obj: dict) -> RoutingConfig:
    try:
        if obj.get("version") == 1:
            obj = _v1_to_v2(obj)
        off = ()
        if obj.get("version") == 2:
            obj, off = _v2_to_v3(obj)
        if obj.get("version") == 3:
            obj = {**obj, "version": 4, "settings": []}
        if obj.get("version") == 4:
            obj = {**obj, "version": 5, "downstreams": [{**d, "advisor": True} for d in obj["downstreams"]]}
        if obj.get("version") != 5:
            raise ValueError("unsupported config version")
        if len(obj["downstreams"]) > MAX_DOWNSTREAMS:
            raise ValueError(f"at most {MAX_DOWNSTREAMS} downstreams")
        dss = []
        for d in obj["downstreams"]:
            dss.append(_downstream(d, [x.id for x in dss]))
        ts = ("anthropic",) + tuple(d.id for d in dss)
        if obj["lever"] not in LEVER_MODES:
            _track(obj["lever"], ts, "lever")
        otherwise = _track(obj["otherwise"], ts, "otherwise")
        rules = tuple(Rule(r["name"], r["kind"], bool(r["enabled"]), r.get("text", ""), r.get("builtin"))
                      for r in obj["rules"])
        if not any(r.builtin == "servertool" for r in rules):  # configs saved before it existed get it on top
            rules = (SERVERTOOL_RULE,) + rules
        if any(r.kind not in ("dsl", "classifier") for r in rules):
            raise ValueError("rule kind must be dsl or classifier")
        for b in ("tags", "classifier", "tier"):
            if not any(r.builtin == b for r in rules):
                raise ValueError(f"built-in rule '{b}' cannot be deleted")
        if sum(r.kind == "classifier" for r in rules) != 1:
            raise ValueError("there must be exactly one classifier rule")
        if len({r.name for r in rules}) != len(rules):
            raise ValueError("rule names must be unique")
        for r in rules:  # a switched-off rule may name a downstream that's gone; switching it on re-checks
            for ln in r.lines if r.enabled else ():
                _track(ln.track, ts, f"rule '{r.name}'")
        c = obj["classifier"]
        labels = tuple(Label(l["name"], l["description"], _track(l["track"], ts, "label track")) for l in c["labels"])
        thr = _unit(c["threshold"], "classifier threshold")
        if c["upstream"] != "none":
            _track(c["upstream"], ts[1:], "classifier upstream")
        d = obj["doubling"]
        sh = d["shadow"]
        if set(sh) != set(ts):
            raise ValueError("doubling shadow must cover every track")
        for t, o in sh.items():
            if o is not None and (o not in ts or o == t):
                raise ValueError(f"doubling shadow for {t} must be another track or none")
        dbl = Doubling(_unit(d["rate"], "doubling rate"), str(d["tag"]), {t: sh[t] for t in ts})
        import overrides  # local: overrides imports routing, so the module can't import it at the top
        settings = overrides.settings_from_json(obj.get("settings", []), tuple(dss))
        return RoutingConfig(obj["lever"], otherwise, rules,
                             ClassifierSettings(str(c["model"]), thr,
                                                float(_num(c["timeout_s"], 0.1, 30, "classifier time limit must be 0.1–30 s")), labels,
                                                c["upstream"]), dbl, tuple(dss), bool(obj.get("seeded", False)), off, settings,
                             prompts_from_json(obj.get("prompts", [])), _auto_from_json(obj.get("auto"), dss))
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"bad config: {e!r}") from None


def config_to_json(cfg: RoutingConfig) -> dict:
    import overrides  # local: overrides imports routing, so the module can't import it at the top
    return {
        "version": 5, "settings": overrides.settings_to_json(cfg.settings),
        "prompts": [{"name": n, "text": t} for n, t in cfg.prompts],
        "lever": cfg.lever, "otherwise": cfg.otherwise, "seeded": cfg.seeded,
        "rules": [{"name": r.name, "kind": r.kind, "enabled": r.enabled, "text": r.text, "builtin": r.builtin}
                  for r in cfg.rules],
        "classifier": {"model": cfg.classifier.model, "threshold": cfg.classifier.threshold,
                       "timeout_s": cfg.classifier.timeout_s, "upstream": cfg.classifier.upstream,
                       "labels": [{"name": l.name, "description": l.description, "track": l.track}
                                  for l in cfg.classifier.labels]},
        "doubling": {"rate": cfg.doubling.rate, "tag": cfg.doubling.tag,
                     "shadow": dict(cfg.doubling.shadow)},
        "downstreams": [asdict(d) for d in cfg.downstreams],
        "auto": {"execution": cfg.auto.execution, "planning_skills": list(cfg.auto.planning_skills),
                 "execution_skills": list(cfg.auto.execution_skills), "plan_dirs": list(cfg.auto.plan_dirs),
                 "threshold": cfg.auto.threshold, "train_cap": cfg.auto.train_cap},
    }


@dataclass
class Decision:
    track: str
    reason: str
    explanation: list
    classified: ClassResult | None = None
    auto: tuple | None = None  # (phase, why) for an auto decision, so the handler can record it without re-deciding


def route(cfg: RoutingConfig, f: Features, classify: Callable[[Features, ClassifierSettings], ClassResult],
          auto: Callable[[Features], tuple] | None = None) -> Decision:
    st = next((r for r in cfg.rules if r.builtin == "servertool"), None)
    if cfg.lever != "rules" and f.servertool and st and st.enabled and st.text.strip() == SERVERTOOL_RULE.text:
        # the lever can't send these anywhere useful: only Anthropic runs its own tools
        return Decision("anthropic", "rule:" + st.name, [{"rule": st.name, "outcome": "Anthropic-only tool, lever overridden → anthropic", "decided": True}])
    if cfg.lever == "auto":
        # the tags of the message that opened the turn, not of the latest human text: mid-turn that can be text
        # Claude Code appended to a tool result, which must not tag the turn. Only the exact lead tags count.
        tags = lead_tags(turn_text(f.messages))
        tagged = "planning" in tags or "execution" in tags
        if tagged:
            got = ph, why = ("planning" if "planning" in tags else "execution"), "tag"
        else:
            got = ph, why = auto(f) if auto else ("none", "no planning signal")  # recorded as decided ("none" stays)
        ph = "planning" if ph == "planning" else "execution"
        track = "anthropic" if ph == "planning" else exec_track(cfg)
        return Decision(track, "tag" if tagged else "auto:" + ph,
                        [{"rule": "Auto", "outcome": f"{ph} ({why}) → {track}", "decided": True}], auto=got)
    if cfg.lever != "rules":
        return Decision(cfg.lever, "lever", [{"rule": "Lever", "outcome": f"sending everything to {cfg.lever}", "decided": True}])
    cs = cfg.classifier
    by_label = {l.name.lower(): l.track for l in cs.labels}
    memo = []

    def result():
        if not memo:
            memo.append(classify(f, cs))
        return memo[0]

    def confident(r):
        return r.label is not None and r.confidence is not None and r.confidence >= cs.threshold \
            and r.label.lower() in by_label

    on = cs.upstream != "none" and any(r.kind == "classifier" and r.enabled for r in cfg.rules)

    def label():
        if f.tier in ("opus", "fable") or not on:
            return "none"
        r = result()
        return r.label if confident(r) else "none"

    out, track, reason = [], None, ""
    for rule in cfg.rules:
        item = {"rule": rule.name, "outcome": "", "decided": False}
        out.append(item)
        if track:
            item["outcome"] = "not reached"
        elif not rule.enabled or (rule.kind == "classifier" and not on):  # upstream "none": as if switched off
            item["outcome"] = "switched off"
        elif rule.kind == "dsl":
            item["outcome"] = "no match"
            for n, ln in enumerate(rule.lines, 1):
                if evaluate(ln.cond, lambda name: f.get(name, label)):
                    track, item["decided"] = ln.track, True
                    reason = f"rule:{rule.name}" + (f":{n}" if len(rule.lines) > 1 else "")
                    item["outcome"] = f"line {n} matched → {track}" + (
                        " (%s)" % " ".join("#" + t for t in sorted(f.tags)) if f.tags and "tag" in fields_of(ln.cond) else "")
                    break
        elif f.tier in ("opus", "fable"):
            item["outcome"] = f"skipped: main session ({f.tier})"
        else:
            r = result()
            pct = f"{round((r.confidence or 0) * 100)}%"
            if confident(r):
                track, item["decided"] = by_label[r.label.lower()], True
                reason, item["outcome"] = f"about:{r.label}", f"{r.label}, {pct} sure → {track}"
            elif r.label is None:
                item["outcome"] = r.note
            else:
                item["outcome"] = f"{r.label}, {pct} sure, below {round(cs.threshold * 100)}%"
    if not track:
        track, reason = cfg.otherwise, "default"
        out.append({"rule": "Otherwise", "outcome": f"→ {track}", "decided": True})
    return Decision(track, reason, out, memo[0] if memo else None)
