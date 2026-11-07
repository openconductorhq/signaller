"""Auto mode helpers: planning/execution phase from the message history, a linear text scorer, old-thinking drop."""
import collections
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Callable, Optional

FORMAT = "signaller-auto-1"
CMD_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
WORD_RE = re.compile(r"[a-z0-9_]+")
EDIT_TOOLS = ("Edit", "Write", "NotebookEdit")


@dataclass(frozen=True)
class AutoSettings:
    execution: Optional[str] = None
    planning_skills: tuple = ("superpowers:brainstorming", "superpowers:writing-plans")
    execution_skills: tuple = ("superpowers:subagent-driven-development", "superpowers:executing-plans")
    plan_dirs: tuple = ("docs/superpowers/specs/", "docs/superpowers/plans/", "/.claude/projects/", "/private/tmp/claude-", "/.superpowers/")
    threshold: float = 0.8


class BoundedDict(collections.OrderedDict):
    """An insertion-ordered map that drops its oldest entry past `cap` (the small per-turn caches use it)."""

    def __init__(self, cap: int):
        super().__init__()
        self.cap = cap

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if len(self) > self.cap:
            self.popitem(last=False)


def text_hash(text: str) -> str:
    """The sha256 of a text, tolerant of lone surrogates (turn keys, cache keys, the score memo)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def blocks(msg) -> list:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def human_text(msg) -> Optional[str]:
    """Joined human text of a user message, None when it has none (same filter as routing.latest_human_text)."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    texts = [b["text"] for b in blocks(msg)
             if b.get("type") == "text" and isinstance(b.get("text"), str)
             and not b["text"].strip().startswith("<system-reminder>")]
    return "\n".join(texts) if texts else None


def _last_user(messages: list, new_turn: bool) -> int:
    """Backwards scan for the latest user message with human text; `new_turn` also skips ones carrying a tool_result."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if human_text(m) is not None and not (new_turn and any(b.get("type") == "tool_result" for b in blocks(m))):
            return i
    return -1


def human_index(messages: list) -> int:
    return _last_user(messages, new_turn=False)


def turn_index(messages: list) -> int:
    """Index of the message that opened the current human turn: the latest user message with human text and no
    tool_result. Claude Code appends text to a tool_result message (a skill's body, a task notification, an
    interrupt's new prompt); that continues the turn rather than starting one. -1 if none."""
    return _last_user(messages, new_turn=True)


def turn_text(messages: list) -> str:
    """Human text of the message that opened the current turn ("" when none): in auto, the lead tags are read from
    this, not from the latest human text, which mid-turn may be text Claude Code appended to a tool result."""
    i = turn_index(messages)
    return (human_text(messages[i]) or "") if i >= 0 else ""


def phase(messages: list, s: AutoSettings, score: Optional[Callable[[str], float]]) -> tuple:
    """(phase, why) over a message list: planning opens at a planning skill (typed or called) or a human message the
    model scores high, and ends for good at an execution skill (typed or called) or the first file edit outside the
    plan folders. One way: end events count only after planning opened. The model scores only a message that could
    open a turn (no tool_result), never text Claude Code appended to one (a skill's body, a task notification)."""
    opened = ended = None
    for msg in messages:
        if ended:
            break
        text = human_text(msg)
        if text is not None:
            m = CMD_RE.search(text)
            if m and m.group(1) in s.planning_skills and not opened:
                opened = "phase: " + m.group(1)
            elif m and m.group(1) in s.execution_skills and opened:
                ended = "phase ended: " + m.group(1)
            elif not opened and score is not None \
                    and not any(b.get("type") == "tool_result" for b in blocks(msg)):
                p = score(text)
                if p >= s.threshold:
                    opened = "model %.2f" % p
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for b in blocks(msg):
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            if b.get("name") == "Skill":
                sk = inp.get("skill")
                if sk in s.planning_skills:
                    opened = opened or "phase: " + sk
                elif sk in s.execution_skills and opened:
                    ended = "phase ended: " + sk
            elif b.get("name") in EDIT_TOOLS and opened:
                fp = inp.get("file_path") or inp.get("notebook_path")  # NotebookEdit's is notebook_path
                if isinstance(fp, str) and not any(d in fp for d in s.plan_dirs):
                    ended = "phase ended: edit " + fp
            if ended:
                break
    if ended:
        return "execution", ended
    if opened:
        return "planning", opened
    return "none", "no planning signal"


def features(text: str) -> set:
    """What Model scores: binary [a-z0-9_]+ unigrams and space-joined bigrams of the first 20000 characters.
    The offline trainer (tools/auto_train.py) uses the same function, so training and serving never diverge."""
    t = WORD_RE.findall(text[:20000].lower())
    return set(t) | {a + " " + b for a, b in zip(t, t[1:])}


class Model:
    MEMO = 2048  # remembered scores: keyed on the text's hash, never the text (2048 of those would be tens of MB)

    def __init__(self, bias: float, weights: dict):
        self.bias, self.weights = bias, weights
        self._memo = BoundedDict(self.MEMO)  # text_hash -> score

    @classmethod
    def load(cls, path: str) -> "Model":
        with open(path) as f:
            try:
                d = json.load(f)
            except ValueError as e:
                raise ValueError("not JSON: %s" % e)
        w = d.get("weights") if isinstance(d, dict) else None
        if (not isinstance(d, dict) or d.get("format") != FORMAT or not isinstance(w, dict)
                or isinstance(d.get("bias"), bool) or not isinstance(d.get("bias"), (int, float))
                or not all(isinstance(k, str) and isinstance(v, (int, float)) and not isinstance(v, bool) for k, v in w.items())):
            raise ValueError("not a %s model" % FORMAT)
        try:
            bias, weights = float(d["bias"]), {k: float(v) for k, v in w.items()}
        except OverflowError:  # an integer too big for a float
            raise ValueError("weight out of range")
        if not all(math.isfinite(v) for v in (bias, *weights.values())):  # a NaN weight would score every text ~1.0
            raise ValueError("non-finite weight")
        return cls(bias, weights)

    def score(self, text: str) -> float:
        """Score of the first 20000 characters (features reads no further), memoised on their hash."""
        t = text[:20000]
        h = text_hash(t)
        got = self._memo.get(h)
        if got is None:
            self._memo[h] = got = self._score(t)
        return got

    def cache_clear(self) -> None:  # the trainer times cold decisions
        self._memo.clear()

    def _score(self, text: str) -> float:
        z = self.bias + sum(self.weights.get(f, 0.0) for f in features(text))
        return 1 / (1 + math.exp(-max(-60.0, min(60.0, z))))


def from_downstream(b) -> bool:
    """A thinking block Anthropic would reject: one a downstream wrote. Anthropic's are signed (452+ characters
    in the transcripts checked); the downstreams seen leave the signature empty or a 6-character stub."""
    # ponytail: a length check, not a verification; a downstream that sends long fake signatures still 400s
    sig = b.get("signature")
    return b.get("type") == "thinking" and not (isinstance(sig, str) and len(sig) >= 100)


def drop_old_thinking(req: dict) -> bool:
    """Drop a downstream's thinking (empty or short signature) from every assistant message: it can't reach
    Anthropic, which 400s on it — the image fallback needs this mid-turn, after the turn already ran on the
    downstream. Anthropic's own signed thinking and redacted_thinking stay, as does a message the drop would
    leave empty (an empty one 400s); an Anthropic-served turn holds no downstream blocks, so no turn boundary."""
    msgs = req.get("messages")
    if not isinstance(msgs, list):
        return False
    changed = False
    for msg in msgs:
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        if not any(isinstance(b, dict) and from_downstream(b) for b in msg["content"]):
            continue  # the common case: nothing of theirs to drop
        keep = [b for b in msg["content"] if not (isinstance(b, dict) and from_downstream(b))]
        # a message that would be left empty is kept unchanged (an empty one 400s); Anthropic may still reject it
        if keep and len(keep) != len(msg["content"]):
            msg["content"] = keep
            changed = True
    return changed
