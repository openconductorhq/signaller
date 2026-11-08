"""Auto training base: turn Claude Code transcripts into labelling candidates.

Moved out of tools/auto_train.py so the router and later auto-training tasks can import the
replay and candidate logic without pulling in scikit-learn (which only the tool needs).
Reads transcripts from the paths it is given; never touches the live router.
"""
import json
import math
import os
import random
import re
from dataclasses import dataclass
from typing import Iterator, Optional

import autophase
import prices
import routing

LABEL_CHARS = 6000  # text sent per labelling prompt; caps cost
PROMPT_TOKENS = 300  # around the text: the labelling prompt's own instructions
OUT_TOKENS = 5  # the label is one word
FIT_EPOCHS = 200
FIT_LR = 0.5
FIT_L2 = 1e-4

FOLLOW = re.compile(r"^\W*(go|go ahead|ok|okay|yes|yep|yeah|y|no|nope|n|sure|approved?|lgtm|do it|proceed|continue|"
                    r"done|looks (good|great|right)|sounds (good|right)|agree|both|[a-d1-9])\b[\s\S]{0,40}$", re.I)


def follow_on(text: str) -> bool:
    w = text.split()
    return len(w) <= 3 or (len(w) <= 7 and bool(FOLLOW.match(text.strip())))


def split(rows: list) -> tuple:
    ids = sorted({r["session"].split("/")[0] for r in rows})
    random.Random(0).shuffle(ids)
    test = set(ids[int(len(ids) * 0.8):])
    return [r for r in rows if r["session"].split("/")[0] not in test], [r for r in rows if r["session"].split("/")[0] in test]


# --- replay: the messages list the router sees, built from a transcript ---

def _events(path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e


def _is_human(e) -> bool:
    return not e.get("isMeta") and (e.get("promptSource") in ("typed", "queued") or e.get("turnOrigin") == "human")


def replay(path):
    """Yield (messages, human_event) at each human turn of a main thread. Consecutive user events merge into one message and an
    assistant message's events (same id) into one, as Claude Code sends them; a compaction keeps only
    its preserved messages."""
    msgs, uuids, pending = [], [], None

    def flush(reply=False):
        nonlocal pending
        if pending is not None and "<command-name>" in pending["_text"] and not reply:
            pending = None  # a slash command with no reply of its own (/model, /clear ...) made no request
        if pending is not None:
            out = pending
            pending = None
            return [(list(msgs), out)]
        return []

    for e in _events(path):
        t = e.get("type")
        if t == "system" and e.get("subtype") == "compact_boundary":
            keep = set(((e.get("compactMetadata") or {}).get("preservedMessages") or {}).get("uuids") or [])
            pairs = [(m, u) for m, u in zip(msgs, uuids) if u & keep]
            msgs, uuids = [p[0] for p in pairs], [p[1] for p in pairs]
            continue
        if t not in ("user", "assistant") or not isinstance(e.get("message"), dict):
            continue
        if e.get("isSidechain"):
            continue
        m, u = e["message"], e.get("uuid")
        if t == "assistant":
            yield from flush(reply=True)
            mid = m.get("id")
            if msgs and msgs[-1]["role"] == "assistant" and mid and msgs[-1].get("_id") == mid:
                msgs[-1]["content"] += autophase.blocks(m)
                uuids[-1].add(u)
            else:
                msgs.append({"role": "assistant", "content": autophase.blocks(m), "_id": mid})
                uuids.append({u})
            continue
        blocks = autophase.blocks(m)
        text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text" and isinstance(b.get("text"), str))
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += blocks
            uuids[-1].add(u)
        else:
            msgs.append({"role": "user", "content": blocks})
            uuids.append({u})
        if _is_human(e) or ("<command-name>" in text and not e.get("isMeta")):
            # two human events with no reply between merge into one message (the merge above ran), so the first is
            # decided with the second's text in it, not on its own history — ponytail: the router sees the same
            # message Claude Code sends, and rebuilding it otherwise would need the reply events
            yield from flush()
            pending = dict(e, _text=text)
    yield from flush()


# --- labelling candidates ---

@dataclass(frozen=True)
class Item:
    id: str  # "<session>:<idx>"
    project: str
    session: str
    idx: int
    ts: str
    chars: int


def candidates(project: str, path: str) -> list:
    """Main-thread human turns of one transcript worth labelling: not follow-ons, not leading #tags, not a typed command.
    idx is the turn's position in replay (all turns, so eval can find it again)."""
    out = []
    session = os.path.basename(path)[:-6] if os.path.basename(path).endswith(".jsonl") else os.path.basename(path)
    for idx, (msgs, ev) in enumerate(replay(path)):
        text = ev.get("_text") or ""
        if follow_on(text) or routing.lead_tags(text) or autophase.CMD_RE.search(text):
            continue
        out.append(Item(id="%s:%d" % (session, idx), project=project, session=session, idx=idx,
                        ts=ev.get("timestamp") or "", chars=min(len(text), LABEL_CHARS)))
    return out


def turn_text(path: str, idx: int) -> Optional[str]:
    """The human text of turn idx (human_text of the turn's last message), for serving to the labeller."""
    for i, (msgs, _) in enumerate(replay(path)):
        if i == idx:
            return autophase.human_text(msgs[-1])
    return None


# --- cost, fit and held-out evaluation ---

def estimate_usd(items: list) -> float:
    """Haiku cost of labelling these items: chars/4 tokens of text plus the prompt, one short answer each."""
    r = prices.DEFAULT_ANTHROPIC["haiku"]
    return sum((i.chars / 4 + PROMPT_TOKENS) * r["in"] + OUT_TOKENS * r["out"] for i in items) / 1e6


def cut(items: list, cap_usd: float) -> list:
    """The newest whole sessions (a session's newest ts) that together fit under the cap; [] if none fit."""
    by = {}
    for i in items:
        by.setdefault(i.session, []).append(i)
    sessions = sorted(by, key=lambda s: max(i.ts for i in by[s]), reverse=True)
    out, spent = [], 0.0
    for s in sessions:
        c = estimate_usd(by[s])
        if spent + c > cap_usd:
            break
        spent += c
        out += by[s]
    return out


def fit(rows: list) -> dict:
    """Stdlib logistic regression over autophase.features: class-balanced, L2, full-batch gradient descent with fixed
    epochs, so no tuning surface. Returns the dict autophase.Model loads (top 5000 weights by |w|, zeros dropped)."""
    # ponytail: rows x epochs in pure Python, no numpy — minutes at 10^5 rows; bigger sets belong to the scikit-learn tool
    n = len(rows)
    if not n:
        return {"format": autophase.FORMAT, "bias": 0.0, "weights": {}}
    n_plan = sum(1 for r in rows if r["label"] == "planning")
    n_exec = n - n_plan
    # balanced, as class_weight="balanced" in tools/auto_train.py: a missing class simply gets no weight
    cw = {"planning": n / (2 * n_plan) if n_plan else 0.0,
          "execution": n / (2 * n_exec) if n_exec else 0.0}
    data = [(autophase.features(r["text"]), 1.0 if r["label"] == "planning" else 0.0, cw[r["label"]]) for r in rows]
    w, bias = {}, 0.0
    for _ in range(FIT_EPOCHS):
        gb, g = 0.0, {}
        for fs, y, c in data:
            z = bias + sum(w.get(f, 0.0) for f in fs)
            e = c * ((1 / (1 + math.exp(-max(-60.0, min(60.0, z))))) - y)
            gb += e
            for f in fs:
                g[f] = g.get(f, 0.0) + e
        bias -= FIT_LR * gb / n
        for f, v in g.items():
            w[f] = w.get(f, 0.0) - FIT_LR * (v / n + FIT_L2 * w.get(f, 0.0))
    keep = sorted(w, key=lambda f: -abs(w[f]))[:5000]
    return {"format": autophase.FORMAT, "bias": round(bias, 6),
            "weights": {f: round(w[f], 6) for f in keep if w[f]}}


def evaluate(rows: list, paths: dict, model: dict, s: autophase.AutoSettings) -> dict:
    """Held-out check: replay each session once, decide every labelled turn with the rules alone and with the model,
    and count planning turns kept and execution turns moved (sent anywhere but planning). Verdict "too few" per the
    plan's Global Constraints (fewer than 30 planning labels in all, or fewer than 5 in the held-out sessions)."""
    m = autophase.Model(model["bias"], model["weights"])
    turns = {}
    for sess in sorted({r["session"] for r in rows if r["session"] in paths}):
        turns[sess] = {i: msgs for i, (msgs, _) in enumerate(replay(paths[sess]))}
    out = {}
    for who, dec in (("rules", None), ("model", m.score)):
        kept = moved = 0
        for r in rows:
            msgs = turns.get(r["session"], {}).get(r["idx"])
            if msgs is None:
                continue
            d = autophase.phase(msgs, s, dec)[0]
            if r["label"] == "planning":
                kept += d == "planning"
            elif r["label"] == "execution":
                moved += d != "planning"
        out[who] = {"planning_kept": kept, "execution_moved": moved}
    planning = sum(1 for r in rows if r["session"] in paths and r["label"] == "planning")
    execution = sum(1 for r in rows if r["session"] in paths and r["label"] == "execution")
    total_plan = sum(1 for r in rows if r["label"] == "planning")
    gain = out["model"]["planning_kept"] - out["rules"]["planning_kept"]
    verdict = "too few" if total_plan < 30 or planning < 5 else (
        "better" if gain > 0
        and out["rules"]["execution_moved"] - out["model"]["execution_moved"] <= gain else "no better")
    out.update({"planning": planning, "execution": execution, "verdict": verdict})
    return out


# --- project listing ---

def _first_cwd(path) -> Optional[str]:
    """cwd from the first 50 lines of a transcript, sessions.find_transcript-style; None if absent or unreadable."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(50), f):
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if isinstance(o, dict) and o.get("cwd"):
                    return o["cwd"]
    except OSError:
        pass
    return None


def projects(claude_dir: str) -> list:
    """One dict per <claude_dir>/projects/<dir> with at least one top-level *.jsonl:
    {"dir", "name" (basename of the first cwd found, else dir), "sessions", "newest" (max file mtime)}. Cheap: no replay."""
    root = os.path.join(claude_dir, "projects")
    try:
        dirs = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for d in dirs:
        p = os.path.join(root, d)
        try:
            files = sorted(f for f in os.listdir(p) if f.endswith(".jsonl") and os.path.isfile(os.path.join(p, f)))
        except OSError:
            continue
        if not files:
            continue
        cwd, newest = None, 0
        for f in files:
            fp = os.path.join(p, f)
            try:
                newest = max(newest, int(os.stat(fp).st_mtime))
            except OSError:
                pass
            if cwd is None:
                cwd = _first_cwd(fp)
        out.append({"dir": d, "name": os.path.basename(cwd) if cwd else d, "sessions": len(files), "newest": newest})
    return out
