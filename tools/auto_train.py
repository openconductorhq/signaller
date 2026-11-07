"""Offline: train auto mode's planning scorer and evaluate auto against today's rules.

Needs scikit-learn (run it from a scratch venv: python3 -m venv V && V/bin/pip install scikit-learn).
The router never imports this file. It reads labels and transcripts from the paths given and
writes only where told (--out, --model, --report); keep both out of the repo.

  auto_train.py train --labels L --out DIR/auto_model.json
  auto_train.py eval  --labels L --transcripts DIR [--model M] [--report eval.md] [--threshold T] [--plan-dirs a,b]
  auto_train.py selftest

Labels (jsonl): {session, kind: main|subagent|teammate, idx, text, label: planning|execution}.
A main row is the idx-th human turn of <DIR>/<session>.jsonl; a subagent/teammate row's session
is "<parent>/<agent file stem>" and its idx is 0 (the brief). Sessions split 80/20 by the parent
session id (seed 0), the same split for train and eval.
"""
import argparse
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import autophase  # noqa: E402
from keys import write_json  # noqa: E402

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
            yield from flush()  # ponytail: two human events with no reply between → first one decided on its own history
            pending = dict(e, _text=text)
    yield from flush()


def brief(path):
    """([first user message], its text) of a subagent/teammate transcript: the brief, before any reply."""
    msg = {"role": "user", "content": []}
    for e in _events(path):
        if e.get("type") == "assistant":
            break
        if e.get("type") == "user" and isinstance(e.get("message"), dict):
            msg["content"] += autophase.blocks(e["message"])
    text = autophase.human_text(msg)
    return ([msg], text) if text else None


def agent_kind(path, text) -> str:
    try:
        with open(path[:-6] + ".meta.json") as f:
            if json.load(f).get("taskKind") == "in_process_teammate":
                return "teammate"
    except (OSError, ValueError, AttributeError):
        pass
    return "teammate" if text.lstrip().startswith("<teammate-message") else "subagent"


# --- train ---

def fit(rows: list, C=1.0, top=5000):
    from sklearn.feature_extraction.text import CountVectorizer
    from sklearn.linear_model import LogisticRegression
    vec = CountVectorizer(analyzer=lambda t: sorted(autophase.features(t)), binary=True)
    X = vec.fit_transform([r["text"] for r in rows])
    y = [r["label"] == "planning" for r in rows]
    clf = LogisticRegression(C=C, class_weight="balanced", max_iter=5000).fit(X, y)
    names, coef = vec.get_feature_names_out(), clf.coef_[0]
    keep = sorted(range(len(coef)), key=lambda i: -abs(coef[i]))[:top]
    return {"format": autophase.FORMAT, "bias": float(clf.intercept_[0]),
            "weights": {str(names[i]): round(float(coef[i]), 6) for i in keep if coef[i]}}


def train_rows(rows):
    return [r for r in rows if not (r["kind"] == "main" and follow_on(r["text"]))]


def cmd_train(a):
    rows = [json.loads(line) for line in open(a.labels)]
    tr, _ = split(rows)
    model = fit(train_rows(tr))
    write_json(a.out, model)
    p = sum(r["label"] == "planning" for r in train_rows(tr))
    print("trained on %d rows (%d planning), %d weights → %s" % (len(train_rows(tr)), p, len(model["weights"]), a.out))


# --- eval ---

def requests(rows, transcripts):
    """(row, messages) for each held-out labelled row, rebuilt from its transcript."""
    by = {}
    for r in rows:
        by.setdefault(r["session"], []).append(r)
    for sess, rs in sorted(by.items()):
        if "/" in sess:
            parent, stem = sess.split("/", 1)
            b = brief(os.path.join(transcripts, parent, "subagents", stem + ".jsonl"))
            if b:
                yield rs[0], b[0]
            continue
        turns = {i: msgs for i, (msgs, _) in enumerate(replay(os.path.join(transcripts, sess + ".jsonl")))}
        for r in rs:
            if r["idx"] in turns:
                yield r, turns[r["idx"]]


def deciders(model, thr, plan_dirs=None):
    s = autophase.AutoSettings(threshold=thr, **({"plan_dirs": tuple(plan_dirs)} if plan_dirs else {}))
    return {
        "today's rules": lambda r, m: "valar" if r["kind"] != "main" else "anthropic",
        "phase only": lambda r, m: "anthropic" if autophase.phase(m, s, None)[0] == "planning" else "valar",
        "phase + model": lambda r, m: "anthropic" if autophase.phase(m, s, model.score if model else None)[0] == "planning" else "valar",
    }


def tally(results):
    n = len(results)
    pe = sum(lab == "planning" and tr == "valar" for lab, tr, _ in results)
    ea = sum(lab == "execution" and tr == "anthropic" for lab, tr, _ in results)
    off = sum(tr == "valar" and k == "main" for _, tr, k in results)
    return {"n": n, "acc": (n - pe - ea) / n if n else 0, "pe": pe, "ea": ea, "off": off,
            "planning": sum(lab == "planning" for lab, _, _ in results)}


def cmd_eval(a):
    rows = [json.loads(line) for line in open(a.labels)]
    _, test = split(rows)
    model = autophase.Model.load(a.model) if a.model else None
    reqs = list(requests(test, a.transcripts))
    kinds = ("main", "subagent", "teammate")

    def run(thr):
        out, times = {}, {}
        for name, dec in deciders(model, thr, a.plan_dirs).items():
            res, ts = [], []
            for r, m in reqs:
                if model:
                    model.score.cache_clear()  # time a cold decision, not the lru cache
                t0 = time.perf_counter()
                tr = dec(r, m)
                ts.append(time.perf_counter() - t0)
                res.append((r["label"], tr, r["kind"], r))
            out[name], times[name] = res, sorted(ts)
        return out, times

    def summary(res, kind=None):
        return tally([(l, t, k) for l, t, k, _ in res if kind is None or k == kind])

    sweep = {}
    if model:
        phase_pe = summary(run(0.8)[0]["phase only"])["pe"]
        for thr in [x / 100 for x in range(50, 100, 5)] + [0.99]:
            sweep[thr] = summary(run(thr)[0]["phase + model"])
        chosen = min(t for t, s in sweep.items() if s["pe"] <= phase_pe)  # the brief's rule, reported only
    else:
        chosen = 0.8
    thr = a.threshold if a.threshold is not None else chosen
    res, times = run(thr)
    main_n = sum(1 for r, _ in reqs if r["kind"] == "main")

    L = ["# Auto mode evaluation", "",
         "Held-out sessions: %d (%s). Requests: %d — %s. Threshold for phase + model: %.2f%s." % (
             len({r["session"].split("/")[0] for r, _ in reqs}), "20% split by session id, seed 0", len(reqs),
             ", ".join("%s %d (%d planning)" % (k, sum(r["kind"] == k for r, _ in reqs),
                                                 sum(r["kind"] == k and r["label"] == "planning" for r, _ in reqs)) for k in kinds),
             thr, "" if model else " (no model: phase + model = phase only)"),
         "Plan dirs: %s." % ", ".join(a.plan_dirs or autophase.AutoSettings().plan_dirs), "",
         "planning→execution = a planning turn sent to Valar (costs quality); execution→anthropic = an execution turn kept "
         "on Anthropic (costs money). Moved off = main-thread turns today's rules send to Anthropic that this decider sends to Valar.", "",
         "| decider | scope | n | accuracy | planning→execution | execution→anthropic | moved off Anthropic |",
         "|---|---|---|---|---|---|---|"]
    for name, rs in res.items():
        for kind in (None,) + kinds:
            s = summary(rs, kind)
            L.append("| %s | %s | %d | %.1f%% | %d / %d | %d / %d | %s |" % (
                name, kind or "all", s["n"], 100 * s["acc"], s["pe"], s["planning"], s["ea"], s["n"] - s["planning"],
                "%d / %d (%.1f%%)" % (s["off"], main_n, 100 * s["off"] / main_n) if kind in (None, "main") and main_n else "–"))
    L += ["", "## Decision time per request (phase() + scoring, cold scorer cache)", "",
          "| decider | mean | p99 | max |", "|---|---|---|---|"]
    for name, ts in times.items():
        if ts:
            L.append("| %s | %.3f ms | %.3f ms | %.3f ms |" % (name, 1000 * sum(ts) / len(ts), 1000 * ts[int(0.99 * (len(ts) - 1))], 1000 * ts[-1]))
    if sweep:
        L += ["", "## Threshold sweep (phase + model, held-out)", "",
              "| threshold | accuracy | planning→execution | execution→anthropic | moved off |", "|---|---|---|---|---|"]
        for t, s in sweep.items():
            L.append("| %.2f | %.1f%% | %d | %d | %d |" % (t, 100 * s["acc"], s["pe"], s["ea"], s["off"]))
        L += ["", "Brief's rule (lowest threshold with planning→execution ≤ phase only's = %d): **%.2f** — set it in the console's Auto section." % (phase_pe, chosen)]
    for name, rs in res.items():
        bad, by = [r for l, t, k, r in rs if l == "planning" and t == "valar"], {}
        L += ["", "## Remaining planning→execution misroutes: %s (%d)" % (name, len(bad)), ""]
        for r in bad:
            by.setdefault(r["session"].split("/")[0], []).append(r)
        spread = [g[i] for i in range(len(bad)) for g in by.values() if i < len(g)]  # one per session first
        for r in spread[:10]:
            L.append("- `%s` %s #%s: %s" % (r["kind"], r["session"][:13], r["idx"], " ".join(clean(r["text"]).split())[:200].replace("|", "/")))
    text = "\n".join(L) + "\n"
    if a.report:
        with open(a.report, "w") as f:
            f.write(text)
    print(text)


def clean(text):
    text = re.sub(r"<system-reminder>.*?</system-reminder>|<local-command-[a-z]+>.*?</local-command-[a-z]+>", "", text, flags=re.S)
    text = re.sub(r"<command-(message|args)>.*?</command-\1>", "", text, flags=re.S)
    return re.sub(r"</?command-name>", "", text).strip()


def selftest():
    import tempfile
    rows = [{"session": "s%d" % i, "kind": "main", "idx": 0, "text": t, "label": l} for i, (t, l) in enumerate([
        ("let us brainstorm the design and write a spec", "planning"),
        ("brainstorm options then refine the spec", "planning"),
        ("write the implementation plan for the cache", "planning"),
        ("fix the failing test in the parser", "execution"),
        ("run the tests and push the branch", "execution"),
        ("deploy the fix to staging and check the logs", "execution")])]
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "auto_model.json")
        with open(p, "w") as f:
            json.dump(fit(rows), f)
        m = autophase.Model.load(p)
    plan, ex = m.score("brainstorm a spec for the plan"), m.score("fix the test and push")
    assert plan > 0.5 > ex, (plan, ex)
    assert autophase.features("Fix the TEST") == {"fix", "the", "test", "fix the", "the test"}
    assert follow_on("yes") and follow_on("2. go ahead") and not follow_on("let us brainstorm the design and write a spec")
    print("selftest ok (planning %.2f, execution %.2f)" % (plan, ex))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("train")
    t.add_argument("--labels", required=True)
    t.add_argument("--out", required=True)
    e = sub.add_parser("eval")
    e.add_argument("--labels", required=True)
    e.add_argument("--transcripts", required=True)
    e.add_argument("--model")
    e.add_argument("--report")
    e.add_argument("--threshold", type=float, help="override the chosen threshold for the main table")
    e.add_argument("--plan-dirs", type=lambda v: [d for d in v.split(",") if d],
                   help="comma-separated plan_dirs to try instead of the default (as the console setting would)")
    sub.add_parser("selftest")
    a = ap.parse_args()
    {"train": cmd_train, "eval": cmd_eval, "selftest": lambda _: selftest()}[a.cmd](a)
