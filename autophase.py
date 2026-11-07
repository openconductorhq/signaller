"""Auto mode helpers: planning/execution phase from the message history, a linear text scorer, old-thinking drop."""
import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
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
    plan_dirs: tuple = ("docs/superpowers/specs/", "docs/superpowers/plans/", "/.claude/projects/", "/tmp/claude-", "/.superpowers/")
    threshold: float = 0.8


def _blocks(msg) -> list:
    c = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def human_text(msg) -> Optional[str]:
    """Joined human text of a user message, None when it has none (same filter as routing.latest_human_text)."""
    if not isinstance(msg, dict) or msg.get("role") != "user":
        return None
    texts = [b["text"] for b in _blocks(msg)
             if b.get("type") == "text" and isinstance(b.get("text"), str)
             and not b["text"].strip().startswith("<system-reminder>")]
    return "\n".join(texts) if texts else None


def human_index(messages: list) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if human_text(messages[i]) is not None:
            return i
    return -1


def turn_index(messages: list) -> int:
    """Index of the message that opened the current human turn: the latest user message with human text and no
    tool_result. Claude Code appends text to a tool_result message (a skill's body, a task notification, an
    interrupt's new prompt); that continues the turn rather than starting one. -1 if none."""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if human_text(m) is not None and not any(b.get("type") == "tool_result" for b in _blocks(m)):
            return i
    return -1


def phase(messages: list, s: AutoSettings, score: Optional[Callable[[str], float]]) -> tuple:
    opened = ended = None
    for msg in messages:
        if ended:
            break
        text = human_text(msg)
        if text is not None:
            if not opened:
                m = CMD_RE.search(text)
                if m and m.group(1) in s.planning_skills:
                    opened = "phase: " + m.group(1)
                elif score is not None:
                    p = score(text)
                    if p >= s.threshold:
                        opened = "model %.2f" % p
            continue
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for b in _blocks(msg):
            if b.get("type") != "tool_use":
                continue
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            if b.get("name") == "Skill":
                sk = inp.get("skill")
                if sk in s.planning_skills:
                    opened = opened or "phase: " + sk
                elif sk in s.execution_skills:
                    ended = "phase ended: " + sk
            elif b.get("name") in EDIT_TOOLS:
                fp = inp.get("file_path")
                if isinstance(fp, str) and not any(d in fp for d in s.plan_dirs):
                    ended = "phase ended: edit " + fp
            if ended:
                break
    if ended:
        return "execution", ended
    if opened:
        return "planning", opened
    return "none", "no planning signal"


class Model:
    def __init__(self, bias: float, weights: dict):
        self.bias, self.weights = bias, weights
        self.score = lru_cache(maxsize=2048)(self._score)

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

    def _score(self, text: str) -> float:
        t = WORD_RE.findall(text.lower()[:20000])
        feats = set(t) | {a + " " + b for a, b in zip(t, t[1:])}
        z = self.bias + sum(self.weights.get(f, 0.0) for f in feats)
        return 1 / (1 + math.exp(-max(-60.0, min(60.0, z))))


def from_downstream(b) -> bool:
    """A thinking block Anthropic would reject: one a downstream wrote. Anthropic's are signed (452+ characters
    in the transcripts checked); the downstreams seen leave the signature empty or a 6-character stub."""
    # ponytail: a length check, not a verification; a downstream that sends long fake signatures still 400s
    sig = b.get("signature")
    return b.get("type") == "thinking" and not (isinstance(sig, str) and len(sig) >= 100)


def drop_old_thinking(req: dict) -> bool:
    """Drop a downstream's thinking from messages before the current turn (turn_index); Anthropic's own signed
    thinking and redacted_thinking stay, as does everything in the current turn (a tool loop needs it)."""
    msgs = req.get("messages")
    if not isinstance(msgs, list):
        return False
    changed = False
    for msg in msgs[:max(turn_index(msgs), 0)]:
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        keep = [b for b in msg["content"] if not (isinstance(b, dict) and from_downstream(b))]
        # a message that would be left empty is kept unchanged (an empty one 400s); Anthropic may still reject it
        if keep and len(keep) != len(msg["content"]):
            msg["content"] = keep
            changed = True
    return changed
