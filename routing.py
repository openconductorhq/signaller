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
TRACKS = ("anthropic", "local")


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


def is_routed(method: str, path: str) -> bool:
    return method == "POST" and path.split("?", 1)[0] in ROUTED_PATHS


def tier_of(model: str) -> str:
    m = model.lower()
    return "haiku" if "haiku" in m else "sonnet" if "sonnet" in m else "opus" if "opus" in m else "other"


class Features:
    def __init__(self, text, tags, model, tokens, subagent, turn_key):
        self.text, self.tags, self.model, self.tokens = text[:TEXT_CAP], tags, model, tokens
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
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            return None
        tags = frozenset(t.lower() for t in TAG_RE.findall(text))
        return cls(text, tags, model, len(body) // 4, sub, hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest())

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


@dataclass(frozen=True)
class Rule:
    name: str
    kind: str  # "dsl" | "classifier"
    enabled: bool
    text: str
    builtin: str | None = None  # "tags" | "classifier" | "tier" | None
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
    when_local: bool
    when_anthropic: bool


@dataclass(frozen=True)
class RoutingConfig:
    lever: str
    otherwise: str
    rules: tuple[Rule, ...]
    classifier: ClassifierSettings
    doubling: Doubling


DEFAULT_CONFIG = RoutingConfig(
    lever="rules", otherwise="anthropic",
    rules=(
        Rule("Tags in your message", "dsl", True, "when tag is plan go anthropic\nwhen tag is local go local", "tags"),
        Rule("What the prompt is about", "classifier", True, "", "classifier"),
        Rule("Model the request asked for", "dsl", True,
             "when tier is opus go anthropic\nwhen tier is sonnet or tier is haiku go local", "tier"),
    ),
    classifier=ClassifierSettings("gemma-haiku", 0.7, 1.5, (
        Label("planning", "designing, specifying or planning work", "anthropic"),
        Label("design & review", "judging, reviewing or critiquing code or ideas", "anthropic"),
        Label("coding", "writing, editing, fixing or running code", "local"),
        Label("chit-chat", "greetings, thanks, small talk", "local"),
    )),
    doubling=Doubling(0, "", True, True),
)


def _unit(v, what):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 1:
        raise ValueError(f"{what} must be 0–1")
    return float(v) if isinstance(v, float) else v


def _track(v, what):
    if v not in TRACKS:
        raise ValueError(f"{what} must be anthropic or local")
    return v


def config_from_json(obj: dict) -> RoutingConfig:
    try:
        if obj.get("version") != 1:
            raise ValueError("unsupported config version")
        if obj["lever"] not in ("anthropic", "rules", "local"):
            raise ValueError("lever must be anthropic, rules or local")
        otherwise = _track(obj["otherwise"], "otherwise")
        rules = tuple(Rule(r["name"], r["kind"], bool(r["enabled"]), r.get("text", ""), r.get("builtin"))
                      for r in obj["rules"])
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
        d = obj["doubling"]
        dbl = Doubling(_unit(d["rate"], "doubling rate"), str(d["tag"]), bool(d["when_local"]), bool(d["when_anthropic"]))
        return RoutingConfig(obj["lever"], otherwise, rules,
                             ClassifierSettings(str(c["model"]), thr, float(c["timeout_s"]), labels), dbl)
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"bad config: {e!r}") from None


def config_to_json(cfg: RoutingConfig) -> dict:
    return {
        "version": 1, "lever": cfg.lever, "otherwise": cfg.otherwise,
        "rules": [{"name": r.name, "kind": r.kind, "enabled": r.enabled, "text": r.text, "builtin": r.builtin}
                  for r in cfg.rules],
        "classifier": {"model": cfg.classifier.model, "threshold": cfg.classifier.threshold,
                       "timeout_s": cfg.classifier.timeout_s,
                       "labels": [{"name": l.name, "description": l.description, "track": l.track}
                                  for l in cfg.classifier.labels]},
        "doubling": {"rate": cfg.doubling.rate, "tag": cfg.doubling.tag,
                     "when_local": cfg.doubling.when_local, "when_anthropic": cfg.doubling.when_anthropic},
    }


@dataclass
class Decision:
    track: str
    reason: str
    explanation: list
    classified: ClassResult | None = None


def route(cfg: RoutingConfig, f: Features, classify: Callable[[Features, ClassifierSettings], ClassResult]) -> Decision:
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
        if f.tier == "opus" or not on:
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
        elif f.tier == "opus":
            item["outcome"] = "skipped: main session (opus)"
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
