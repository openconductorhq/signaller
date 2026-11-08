"""Auto training base: turn Claude Code transcripts into labelling candidates.

Moved out of tools/auto_train.py so the router and later auto-training tasks can import the
replay and candidate logic without pulling in scikit-learn (which only the tool needs).
Reads transcripts from the paths it is given; never touches the live router.
"""
import concurrent.futures
import json
import math
import os
import random
import re
import shutil
import threading
import time
from collections import Counter
from dataclasses import dataclass

import autophase
import prices
import routing
import sessions

LABEL_CHARS = 6000  # text sent per labelling prompt; caps cost
PROMPT_TOKENS = 300  # around the text: the labelling prompt's own instructions
OUT_TOKENS = 5  # the label is one word
FIT_EPOCHS = 200
FIT_LR = 0.5
FIT_L2 = 1e-4

FOLLOW = re.compile(r"^\W*(go|go ahead|ok|okay|yes|yep|yeah|y|no|nope|n|sure|approved?|lgtm|do it|proceed|continue|"
                    r"done|looks (good|great|right)|sounds (good|right)|agree|both|[a-d1-9])\b[\s\S]{0,40}$", re.I)

LABEL_MARKER = "signaller-label-v1"  # the first line of the labeller's system prompt; the router routes on it


class Conflict(ValueError):
    """A plan that would step on a running job; the API answers 409 with it."""


def is_label_request(req) -> bool:
    """True for the labeller's own /v1/messages request: its system text starts with the marker line, so
    the router sends it to Anthropic untouched and stores none of its text."""
    s = req.get("system") if isinstance(req, dict) else None
    if isinstance(s, list):
        s = "".join(b.get("text", "") for b in s if isinstance(b, dict))
    return isinstance(s, str) and s.startswith(LABEL_MARKER)


def follow_on(text: str) -> bool:
    w = text.split()
    return len(w) <= 3 or (len(w) <= 7 and bool(FOLLOW.match(text.strip())))


def split(rows: list) -> tuple:
    ids = sorted({r["session"].split("/")[0] for r in rows})
    random.Random(0).shuffle(ids)
    test = set(ids[int(len(ids) * 0.8):])
    return [r for r in rows if r["session"].split("/")[0] not in test], [r for r in rows if r["session"].split("/")[0] in test]


# --- replay: the messages list the router sees, built from a transcript ---

def events(path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if isinstance(e, dict):
                yield e


def is_human(e) -> bool:
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

    for e in events(path):
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
        if is_human(e) or ("<command-name>" in text and not e.get("isMeta")):
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
    idx is the turn's position in replay (all turns, so eval can find it again).
    ponytail: replay builds the whole messages list in memory, so a GB-sized transcript costs its size; a
    streaming design (deciding from human events alone) is the ceiling-raiser if real ones ever hit it."""
    out = []
    session = os.path.basename(path)[:-6] if os.path.basename(path).endswith(".jsonl") else os.path.basename(path)
    for idx, (msgs, ev) in enumerate(replay(path)):
        text = ev.get("_text") or ""
        if follow_on(text) or routing.lead_tags(text) or autophase.CMD_RE.search(text):
            continue
        out.append(Item(id="%s:%d" % (session, idx), project=project, session=session, idx=idx,
                        ts=ev.get("timestamp") or "", chars=min(len(text), LABEL_CHARS)))
    return out


def turns_at(path: str, idxs) -> dict:
    """{idx: the turn's messages} for the wanted turns of one transcript: one replay, stopping once all are
    found, {} when it is unreadable or a wanted turn is gone (its items then count as failed, its rows unscored)."""
    out = {}
    try:
        for i, (msgs, _) in enumerate(replay(path)):
            if i in idxs:
                out[i] = msgs
                if len(out) == len(idxs):
                    break
    except OSError:
        pass
    return out


# --- cost, fit and held-out evaluation ---

RATE = prices.DEFAULT_ANTHROPIC["haiku"]


def estimate_usd(items: list) -> float:
    """Haiku cost of labelling these items: chars/4 tokens of text plus the prompt, one short answer each."""
    return sum((i.chars / 4 + PROMPT_TOKENS) * RATE["in"] + OUT_TOKENS * RATE["out"] for i in items) / 1e6


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
    want = {}
    for r in rows:
        if r["session"] in paths:
            want.setdefault(r["session"], set()).add(r["idx"])
    turns = {sess: turns_at(paths[sess], idxs) for sess, idxs in want.items()}
    # a held-out row whose transcript vanished or whose turn is gone was never decided: it counts nowhere
    scored = [r for r in rows if r["idx"] in turns.get(r["session"], {})]
    out = {}
    for who, dec in (("rules", None), ("model", m.score)):
        kept = moved = 0
        for r in scored:
            d = autophase.phase(turns[r["session"]][r["idx"]], s, dec)[0]
            if r["label"] == "planning":
                kept += d == "planning"
            elif r["label"] == "execution":
                moved += d != "planning"
        out[who] = {"planning_kept": kept, "execution_moved": moved}
    counts = Counter(r["label"] for r in scored)  # one pass over the label counts
    total_plan = sum(1 for r in rows if r["label"] == "planning")  # the "in all" rule counts every label
    gain = out["model"]["planning_kept"] - out["rules"]["planning_kept"]
    verdict = "too few" if total_plan < 30 or counts["planning"] < 5 else (
        "better" if gain > 0
        and out["rules"]["execution_moved"] - out["model"]["execution_moved"] <= gain else "no better")
    out.update({"planning": counts["planning"], "execution": counts["execution"], "verdict": verdict})
    return out


# --- project listing ---

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
                cwd = sessions.head_fields(fp)[0]
        out.append({"dir": d, "name": os.path.basename(cwd) if cwd else d, "sessions": len(files), "newest": newest})
    return out


# --- the router's training job ---

class Trainer:
    """The console's and the plugin's training job: scans the projects' transcripts for labelling
    candidates, serves the unlabelled ones' text to the labeller (the only place prompt text ever
    leaves), collects the labels and fits the model. One worker thread (a max_workers=1 executor,
    created on the first train call) does every transcript read and every fit, so no request thread
    ever replays a transcript; the shared state below is guarded by `lock`, which the worker holds
    only for short mutations, never during a replay. Labels persist in <config>/training/labels.jsonl
    (0700 dir, 0600 file, ids and labels only, no text), so a restart or a re-plan resumes instead of
    paying for the same prompts again; the folder is deleted when the model is used or the job discarded."""

    def __init__(self, config_dir: str, claude_dir: str, settings):
        self.claude_dir = claude_dir
        self.settings = settings  # callable -> AutoSettings: the cap and the evaluate settings, read at use
        self.training_dir = os.path.join(config_dir, "training")
        self.labels_path = os.path.join(self.training_dir, "labels.jsonl")
        self.lock = threading.Lock()
        self.state, self.job = "idle", None
        self.result, self.error, self.model = None, None, None
        self.projects = None  # the project rows (listdir/stat/head reads, no replay), rebuilt by plan/reset and a stale GET
        self._scanned = False
        self._rows_at = 0.0  # when the rows were last built; a GET rescans when this is over 5 minutes old
        # (path, mtime, size) -> items; worker thread only; bounded, a live transcript keeps making keys
        self._cache = autophase.BoundedDict(4096)
        self._pool = None  # the one worker thread, on the first train call: nobody trains, no thread

    # --- the worker ---

    def _worker(self):
        with self.lock:
            if self._pool is None:
                self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            return self._pool

    def _call(self, fn, *args):
        """Run fn on the worker thread and wait for it; the worker always answers, so this returns."""
        return self._worker().submit(fn, *args).result()

    def _items_for(self, path: str, project: str) -> list:
        """A transcript's candidates, cached per (path, mtime, size); unreadable -> []. Worker only."""
        try:
            st = os.stat(path)
        except OSError:
            return []
        key = (path, st.st_mtime_ns, st.st_size)
        if key not in self._cache:
            try:
                self._cache[key] = candidates(project, path)
            except Exception:  # one broken transcript is skipped, never a dead scan
                self._cache[key] = []
        return self._cache[key]

    def _rows(self, force=False):
        """The project rows (the listing's I/O runs outside the lock); `force` lists the directory again, keeping
        the counts a scan already paid for, so a project born since the last listing can be planned. A status
        call that built rows while the worker's scan did keeps the winner's: a losing listing is never swapped in
        over one that is filling or filled."""
        if force or self.projects is None:
            fresh = {p["dir"]: p for p in self._project_rows()}
            with self.lock:
                if force or self.projects is None:
                    old = {p["dir"]: p for p in (self.projects or [])}
                    for d, p in fresh.items():
                        if d in old:
                            p["prompts"], p["usd"] = old[d]["prompts"], old[d]["usd"]
                    self.projects, self._rows_at = list(fresh.values()), time.time()
        return self.projects

    def _scan(self, force=False):
        try:
            for row in self._rows(force):  # the rows fill in one by one: a GET mid-scan shows progress
                items = []
                for f in self._files(row["dir"]):
                    items += self._items_for(os.path.join(self.claude_dir, "projects", row["dir"], f), row["dir"])
                with self.lock:
                    row["prompts"], row["usd"] = len(items), round(estimate_usd(items), 4)
            with self.lock:
                self._scanned = True
        except Exception as e:  # a scan that dies is a message in the card, never a stuck "scanning"
            with self.lock:
                self.state, self.error = "failed", "%s: %s" % (type(e).__name__, str(e)[:200])
        finally:
            with self.lock:
                if self.state == "scanning":
                    self.state = "ready"

    def _project_rows(self):
        return [{"dir": p["dir"], "name": p["name"], "sessions": p["sessions"], "prompts": None, "usd": None}
                for p in projects(self.claude_dir)]

    def _files(self, d: str) -> list:
        try:
            p = os.path.join(self.claude_dir, "projects", d)
            return sorted(f for f in os.listdir(p) if f.endswith(".jsonl") and os.path.isfile(os.path.join(p, f)))
        except OSError:
            return []

    # --- the public face, for console.py ---

    def start_scan(self) -> None:
        """A GET kicks the worker off when the rows were never built or the last listing is over 5 minutes old;
        never blocks, and never over a job's state."""
        # ponytail: a GET rescans at most every 5 minutes; a directory watcher would notice new projects sooner
        with self.lock:
            stale = time.time() - self._rows_at > 300
            if self.job or (self._scanned and not stale) or self.state not in ("idle", "ready", "failed"):
                return
            self.state = "scanning"
        self._worker().submit(self._scan, stale)

    def status(self) -> dict:
        projects = self._rows()
        with self.lock:
            return {"state": self.state, "projects": [dict(p) for p in projects], "job": self._job_block(),
                    "result": self.result, "error": self.error, "cap": self.settings().train_cap}

    def _job_block(self):  # caller holds the lock
        if not self.job:
            return None
        j = self.job
        pend = [j["items"][i] for i in j["items"] if i not in j["labelled"] and i not in j["failed"]]
        return {"projects": j["projects"], "items": len(j["items"]), "labelled": len(j["labelled"]),
                "failed": len(j["failed"]), "usd_est": j["usd_est"], "usd_spent": j["usd_spent"], "cut": j["cut"],
                "cut_applied": j["cut_applied"], "remaining": len(pend),
                "usd_remaining": round(estimate_usd(pend), 4)}

    def plan(self, dirs, cut: bool) -> dict:
        """Start or resume the job for these project dirs (a re-plan of the same projects and cut choice
        resumes, keeping the labels on disk whose ids still occur; one with progress for anything else,
        or any plan while a fit runs or a result waits, answers Conflict). Returns the job block."""
        if not isinstance(dirs, list) or not dirs or not all(isinstance(d, str) and d for d in dirs):
            raise ValueError("projects must be a list of project dirs")
        self._call(self._plan, dirs, bool(cut))
        with self.lock:
            return self._job_block()

    def _plan(self, dirs, want_cut):
        cap = self.settings().train_cap
        with self.lock:  # the worker runs this: no race with a labelling batch or another plan
            if self.state in ("training", "done"):
                raise Conflict("a training run is fitting or finished; use or discard it first")
            j = self.job
            if self.state == "labelling" and j and (j["labelled"] or j["failed"] or j["usd_spent"]) \
                    and (set(j["projects"]) != set(dirs) or j["cut_applied"] != bool(want_cut)):
                raise Conflict("a training run is in progress; discard it first")
        rows = self._rows(force=True)  # the listing again: a project born since the last scan can be planned
        unknown = [d for d in dirs if d not in {p["dir"] for p in rows}]
        if unknown:
            raise ValueError("no project dir named %s" % unknown[0])
        items, paths = {}, {}
        for d in dirs:
            for f in self._files(d):
                path = os.path.join(self.claude_dir, "projects", d, f)
                # ponytail: two planned projects with the same transcript basename share a session
                # name (and so ids); the offline tool has the same collision, ignore it
                for it in self._items_for(path, d):
                    items[it.id] = it
                    paths[it.session] = path
        full = list(items.values())
        labels, failed, spent = self._read_labels()
        # the cap and the cut count only what still needs paying for: a resume keeps its labelled sessions
        unlab = [i for i in full if i.id not in labels and i.id not in failed]
        usd_unlab = estimate_usd(unlab)
        option = cut(unlab, cap) if usd_unlab > cap else None
        usd_option = estimate_usd(option) if option else 0.0
        applied = want_cut and bool(option)
        chosen = [i for i in full if i.id in labels or i.id in failed] + (option if applied else unlab)
        items = {i.id: i for i in chosen}  # items keeps insertion order; no order list
        with self.lock:
            self.job = {"projects": list(dirs), "items": items, "paths": paths,
                        "labelled": {i: l for i, l in labels.items() if i in items},
                        "failed": {i for i in failed if i in items},
                        "usd_est": round(usd_option if applied else usd_unlab, 4), "usd_spent": round(spent, 4),
                        # ruling A: the newest-sessions-under-cap option when the job's remaining cost
                        # exceeds the cap, applied or not; null when no whole session fits
                        "cut": ({"items": len(option), "usd": round(usd_option, 4)} if option else None),
                        "cut_applied": applied}
            self.state, self.result, self.error, self.model = "labelling", None, None, None

    def next(self, n: int) -> dict:
        """The next n unlabelled items' text — the only API that ever serves prompt text."""
        return self._call(self._next, n)

    def _next(self, n):
        out, served = [], set()
        while len(out) < n:  # a transcript that vanished or turned unreadable fails its items; the rest still serve
            with self.lock:
                job = self.job
                if not job or self.state != "labelling":
                    raise ValueError("no labelling job")
                items, paths = job["items"], job["paths"]
                pend = [i for i in items if i not in job["labelled"] and i not in job["failed"]
                        and i not in served][:n - len(out)]
            if not pend:
                break
            want, bad = {}, []
            for i in pend:  # one replay per transcript serves the whole batch
                it = items[i]
                want.setdefault(it.session, []).append(it)
            for sess, its in want.items():
                got = turns_at(paths.get(sess, ""), {it.idx for it in its})
                for it in its:
                    t = autophase.human_text(got[it.idx][-1]) if it.idx in got else None
                    if t is not None:
                        out.append((it, t))
                        served.add(it.id)
                    else:
                        bad.append(it.id)
            self._mark_failed(bad)
        return {"items": [{"id": it.id, "text": t[:LABEL_CHARS]} for it, t in out]}

    def labels(self, recs, failed, usage) -> dict:
        """Append labeller answers to labels.jsonl and add the batch's cost; all-or-nothing."""
        recs = recs if isinstance(recs, list) else []
        failed = failed if isinstance(failed, list) else []
        with self.lock:
            job = self.job
            if not job:
                raise ValueError("no labelling job")
            for r in failed:
                if not isinstance(r, str) or r not in job["items"]:
                    raise ValueError("unknown id %r" % (r,))
            for r in recs:
                if not isinstance(r, dict) or not isinstance(r.get("id"), str) or r["id"] not in job["items"]:
                    raise ValueError("unknown label id %r" % (r.get("id") if isinstance(r, dict) else r))
                if r.get("label") not in ("planning", "execution"):
                    raise ValueError("label must be planning or execution")
            u = usage if isinstance(usage, dict) else {}
            if any(type(v) in (int, float) and (not math.isfinite(v) or v < 0)
                   for k, v in u.items() if k in ("input_tokens", "output_tokens")):
                raise ValueError("usage token counts must be finite and non-negative")
            usd = self._usage_usd(usage)
            self._append([{"id": r["id"], "label": r["label"]} for r in recs])
            self._fail(failed)
            if usd:
                self._append([{"usd": round(usd, 6)}])
            for r in recs:
                job["labelled"][r["id"]] = r["label"]
                job["failed"].discard(r["id"])
            job["usd_spent"] = round(job["usd_spent"] + usd, 6)
            return self._job_block()

    def _usage_usd(self, usage) -> float:
        u = usage if isinstance(usage, dict) else {}
        tok = lambda k: u.get(k) if type(u.get(k)) in (int, float) else 0
        return (tok("input_tokens") * RATE["in"] + tok("output_tokens") * RATE["out"]) / 1e6

    def finish(self) -> None:
        """Hand the fit to the worker and answer at once (state "training"); the plugin and the card poll for
        the done/failed that lands when the fit ends."""
        with self.lock:
            if not self.job or self.state != "labelling":
                raise ValueError("no labelling job")
            self.state = "training"
        self._worker().submit(self._finish)

    def _finish(self):
        s = self.settings()
        with self.lock:
            job = self.job
            if not job:
                return  # discarded between the request and the worker's turn
            rows = []
            for i, label in list(job["labelled"].items()):
                it = job["items"][i]
                rows.append({"session": it.session, "idx": it.idx, "label": label})
        try:
            train, test = split(rows)
            want = {}
            for r in train:
                want.setdefault(r["session"], set()).add(r["idx"])
            texts = self._turn_texts(want, job["paths"])
            fit_rows = [{"text": texts[(r["session"], r["idx"])], "label": r["label"]} for r in train
                        if (r["session"], r["idx"]) in texts]
            model = fit(fit_rows)
            held = {sess: job["paths"][sess] for sess in {r["session"] for r in test}}
            result = evaluate(rows, held, model, s)
            with self.lock:
                if self.job is job:  # a re-plan since may have replaced the job; its state wins
                    self.model, self.result, self.state = model, result, "done"
        except Exception as e:  # a failure is a message in the card, never a dead router
            with self.lock:
                if self.job is job:
                    self.state, self.error = "failed", "%s" % e

    def _turn_texts(self, wanted, paths) -> dict:
        """{(session, idx): text} for the wanted turns ({session: idxs}), one replay per transcript."""
        out = {}
        for sess, idxs in wanted.items():
            for i, msgs in turns_at(paths.get(sess, ""), idxs).items():
                t = autophase.human_text(msgs[-1])
                if t is not None:
                    out[(sess, i)] = t
        return out

    def use(self) -> dict:
        """The fitted model, for the caller to write as auto_model.json and load. 409-shaped error when
        nothing finished, the held-out check says too few to judge, or the fit has no weights."""
        with self.lock:
            if self.state != "done" or not self.model:
                raise ValueError("no finished model to use")
            if (self.result or {}).get("verdict") == "too few":
                raise ValueError("too few planning prompts to judge; label more first")
            if not self.model.get("weights"):
                raise ValueError("the fitted model has no weights")
            return dict(self.model)

    def reset(self) -> None:
        """Install (use) or drop (discard): training/ goes, the job is forgotten, the project rows are
        listed again on the next GET."""
        with self.lock:
            self.state, self.job, self.result, self.error, self.model = "idle", None, None, None, None
            self.projects, self._scanned, self._rows_at = None, False, 0.0
        shutil.rmtree(self.training_dir, ignore_errors=True)

    # --- labels.jsonl ---

    def _read_labels(self):
        out, failed, spent = {}, set(), 0.0
        try:
            with open(self.labels_path) as f:
                for line in f:
                    try:
                        o = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(o, dict) or not isinstance(o.get("id"), str):
                        if isinstance(o, dict) and isinstance(o.get("usd"), (int, float)):
                            spent += o["usd"]  # a batch's cost, kept so a resume reports real spend
                        continue
                    if o.get("label") in ("planning", "execution"):
                        out[o["id"]] = o["label"]
                    elif o.get("failed"):
                        failed.add(o["id"])
                        out.pop(o["id"], None)
        except OSError:
            pass
        return out, failed, spent

    def _append(self, objs) -> None:  # caller holds the lock
        try:
            os.makedirs(self.training_dir, mode=0o700, exist_ok=True)
            with os.fdopen(os.open(self.labels_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
                for o in objs:
                    f.write(json.dumps(o) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass  # unpersisted labels are re-served by a later next; never kill the job over it

    def _fail(self, ids) -> None:  # caller holds the lock: the one place that marks and appends failures
        job = self.job
        new = [i for i in ids if i in job["items"] and i not in job["failed"]]
        if new:
            job["failed"].update(new)
            self._append([{"id": i, "failed": True} for i in new])

    def _mark_failed(self, ids) -> None:  # worker: an unreadable transcript's items
        if ids:
            with self.lock:
                if self.job:
                    self._fail(ids)
