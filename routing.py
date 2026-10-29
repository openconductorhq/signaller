"""Routing engine: features of a request, user-editable rules, and the decision with its explanation."""
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable

from rulelang import RuleError, evaluate, parse

ROUTED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")
TAG_RE = re.compile(r"(?<!\w)#(\w+)", re.I)
TEXT_CAP = 20_000
TRACKS = ("anthropic", "valar", "local")


def latest_human_text(messages: list) -> str:
    """Text of the most recent user message that has non-system-reminder text."""
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        texts = [
            b["text"] for b in content
            if b.get("type") == "text" and not b["text"].strip().startswith("<system-reminder>")
        ]
        if texts:
            return "\n".join(texts)
    return ""


def session_of(headers, req) -> str | None:
    """Claude Code session id: the header, else metadata.user_id's JSON. Never raises."""
    try:
        sid = headers.get("X-Claude-Code-Session-Id")  # http.client headers are case-insensitive
        if not sid:
            sid = {k.lower(): v for k, v in headers.items()}.get("x-claude-code-session-id")
        if isinstance(sid, str) and sid:
            return sid
        sid = json.loads(req["metadata"]["user_id"])["session_id"]
        return sid if isinstance(sid, str) and sid else None
    except Exception:
        return None


def is_routed(method: str, path: str) -> bool:
    return method == "POST" and path.split("?", 1)[0] in ROUTED_PATHS


def tier_of(model: str) -> str:
    m = model.lower()
    return next((t for t in ("haiku", "sonnet", "opus", "fable") if t in m), "other")


MODEL_TIERS = ("fable", "opus", "sonnet", "haiku")


def track_model(model: str, m: dict) -> str:
    """The model id a non-Anthropic track serves for a request's model; non-claude ids pass through."""
    if not model.startswith("claude-"):
        return model
    return m.get(tier_of(model), m["sonnet"])


WINDOWS = ("asap", "priority", "standard")  # Valar completion windows; flex rejects streaming, batch is async: never sent


def track_window(model: str, windows: dict) -> str:
    """The Valar completion window for a request's model (same slot lookup as track_model)."""
    return windows.get(tier_of(model), windows["sonnet"]) if model.startswith("claude-") else windows["sonnet"]


class Features:
    def __init__(self, text, tags, model, tokens, subagent, turn_key, servertool=False):
        self.text, self.tags, self.model, self.tokens, self.servertool = text[:TEXT_CAP], tags, model, tokens, servertool
        self.tier, self.subagent, self.turn_key = tier_of(model), subagent, turn_key

    @classmethod
    def from_body(cls, body: bytes):
        """None when the body isn't a messages request we can route (passthrough)."""
        try:
            req = json.loads(body)
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
            srv = any(isinstance(t, dict) and t.get("type") not in (None, "custom") for t in req.get("tools") or [])
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return None
        tags = frozenset(t.lower() for t in TAG_RE.findall(text))
        return cls(text, tags, model, len(body) // 4, sub, hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(), srv)

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
    models: dict  # {"valar"|"local": {"sonnet": id, "haiku": id}}
    windows: dict = field(default_factory=lambda: {"valar": {t: "asap" for t in MODEL_TIERS}})


SERVERTOOL_RULE = Rule("Tools only Anthropic can run", "dsl", True, "when servertool go anthropic", "servertool")

DEFAULT_CONFIG = RoutingConfig(
    lever="rules", otherwise="anthropic",
    rules=(
        SERVERTOOL_RULE,
        Rule("Tags in your message", "dsl", True, "when tag is planning go anthropic\nwhen tag is local go local\nwhen tag is execution go valar", "tags"),
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
    doubling=Doubling(0, "", {"anthropic": "valar", "valar": "anthropic", "local": None}),
    models={"valar": {"fable": "zai-org/GLM-5.3", "opus": "zai-org/GLM-5.3", "sonnet": "zai-org/GLM-5.3",
                      "haiku": "deepseek-ai/DeepSeek-V4.1-Flash-1017"},
            "local": {"fable": "claude-sonnet-4-6", "opus": "claude-sonnet-4-6", "sonnet": "claude-sonnet-4-6",
                      "haiku": "claude-haiku-4-5-20251001"}},
)


def _num(v, lo, hi, msg):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:  # nan fails the range too
        raise ValueError(msg)
    return v


def _unit(v, what):
    return _num(v, 0, 1, f"{what} must be 0–1")


def _track(v, what):
    if v not in TRACKS:
        raise ValueError(f"{what} must be anthropic, valar or local")
    return v


def config_from_json(obj: dict) -> RoutingConfig:
    try:
        if obj.get("version") == 1:
            d = obj["doubling"]
            obj = {**obj, "version": 2, "models": {k: dict(v) for k, v in DEFAULT_CONFIG.models.items()},
                   "classifier": {**obj["classifier"], "upstream": "local"},
                   "doubling": {"rate": d["rate"], "tag": d["tag"],
                                "shadow": {"anthropic": "local" if d["when_anthropic"] else None, "valar": None,
                                           "local": "anthropic" if d["when_local"] else None}}}
        if obj.get("version") != 2:
            raise ValueError("unsupported config version")
        if obj["lever"] not in ("anthropic", "rules", "valar", "local"):
            raise ValueError("lever must be anthropic, rules, valar or local")
        otherwise = _track(obj["otherwise"], "otherwise")
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
        c = obj["classifier"]
        labels = tuple(Label(l["name"], l["description"], _track(l["track"], "label track")) for l in c["labels"])
        thr = _unit(c["threshold"], "classifier threshold")
        if c["upstream"] not in ("valar", "local"):
            raise ValueError("classifier upstream must be valar or local")
        d = obj["doubling"]
        sh = d["shadow"]
        if set(sh) != set(TRACKS):
            raise ValueError("doubling shadow must cover every track")
        for t, o in sh.items():
            if o is not None and (o not in TRACKS or o == t):
                raise ValueError(f"doubling shadow for {t} must be another track or none")
        dbl = Doubling(_unit(d["rate"], "doubling rate"), str(d["tag"]), {t: sh[t] for t in TRACKS})
        models = {}
        for t in ("valar", "local"):
            m = obj["models"][t]  # configs from before fable/opus had their own model: those served sonnet's
            models[t] = {"fable": m["sonnet"], "opus": m["sonnet"], **{k: m[k] for k in m if k in MODEL_TIERS}, "haiku": m["haiku"]}
            if not all(isinstance(v, str) and v for v in models[t].values()):
                raise ValueError(f"models.{t} must be non-empty text")
        w = {**{t: "asap" for t in MODEL_TIERS}, **(obj.get("windows") or {}).get("valar", {})}
        for t in MODEL_TIERS:
            if w[t] not in WINDOWS:
                raise ValueError(f"windows.valar.{t} must be asap, priority or standard")
        return RoutingConfig(obj["lever"], otherwise, rules,
                             ClassifierSettings(str(c["model"]), thr,
                                                float(_num(c["timeout_s"], 0.1, 30, "classifier time limit must be 0.1–30 s")), labels,
                                                c["upstream"]), dbl, models, {"valar": {t: w[t] for t in MODEL_TIERS}})
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"bad config: {e!r}") from None


def config_to_json(cfg: RoutingConfig) -> dict:
    return {
        "version": 2, "lever": cfg.lever, "otherwise": cfg.otherwise,
        "rules": [{"name": r.name, "kind": r.kind, "enabled": r.enabled, "text": r.text, "builtin": r.builtin}
                  for r in cfg.rules],
        "classifier": {"model": cfg.classifier.model, "threshold": cfg.classifier.threshold,
                       "timeout_s": cfg.classifier.timeout_s, "upstream": cfg.classifier.upstream,
                       "labels": [{"name": l.name, "description": l.description, "track": l.track}
                                  for l in cfg.classifier.labels]},
        "doubling": {"rate": cfg.doubling.rate, "tag": cfg.doubling.tag,
                     "shadow": dict(cfg.doubling.shadow)},
        "models": {t: dict(m) for t, m in cfg.models.items()},
        "windows": {t: dict(m) for t, m in cfg.windows.items()},
    }


@dataclass
class Decision:
    track: str
    reason: str
    explanation: list
    classified: ClassResult | None = None


def route(cfg: RoutingConfig, f: Features, classify: Callable[[Features, ClassifierSettings], ClassResult]) -> Decision:
    st = next((r for r in cfg.rules if r.builtin == "servertool"), None)
    if cfg.lever != "rules" and f.servertool and st and st.enabled and st.text.strip() == SERVERTOOL_RULE.text:
        # the lever can't send these anywhere useful: only Anthropic runs its own tools
        return Decision("anthropic", "rule:" + st.name, [{"rule": st.name, "outcome": "Anthropic-only tool, lever overridden → anthropic", "decided": True}])
    if cfg.lever != "rules":
        return Decision(cfg.lever, "lever", [{"rule": "Lever", "outcome": f"sending everything to {cfg.lever}", "decided": True}])
    cs = cfg.classifier
    tracks = {l.name.lower(): l.track for l in cs.labels}
    memo = []

    def result():
        if not memo:
            memo.append(classify(f, cs))
        return memo[0]

    def confident(r):
        return r.label is not None and r.confidence is not None and r.confidence >= cs.threshold \
            and r.label.lower() in tracks

    on = any(r.kind == "classifier" and r.enabled for r in cfg.rules)

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
        elif not rule.enabled:
            item["outcome"] = "switched off"
        elif rule.kind == "dsl":
            item["outcome"] = "no match"
            for n, ln in enumerate(rule.lines, 1):
                if evaluate(ln.cond, lambda name: f.get(name, label)):
                    track, item["decided"] = ln.track, True
                    reason = f"rule:{rule.name}" + (f":{n}" if len(rule.lines) > 1 else "")
                    item["outcome"] = f"line {n} matched → {track}"
                    break
        elif f.tier in ("opus", "fable"):
            item["outcome"] = f"skipped: main session ({f.tier})"
        else:
            r = result()
            pct = f"{round((r.confidence or 0) * 100)}%"
            if confident(r):
                track, item["decided"] = tracks[r.label.lower()], True
                reason, item["outcome"] = f"about:{r.label}", f"{r.label}, {pct} sure → {track}"
            elif r.label is None:
                item["outcome"] = r.note
            else:
                item["outcome"] = f"{r.label}, {pct} sure, below {round(cs.threshold * 100)}%"
    if not track:
        track, reason = cfg.otherwise, "default"
        out.append({"rule": "Otherwise", "outcome": f"→ {track}", "decided": True})
    return Decision(track, reason, out, memo[0] if memo else None)
