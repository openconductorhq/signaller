# Auto Training Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let anyone on auto mode train their own planning model from Claude Code projects they pick: the plugin labels prompts with Haiku, the router trains, evaluates and installs the model.

**Architecture:** A new stdlib module `autotrain.py` holds everything about transcripts and training (replay, which prompts to label, cost estimate, logistic fit, evaluation, the single training job and its label file). `signaller.py` owns one `autotrain.Trainer`, `console.py` exposes it under `/ui/api/train…`. The plugin's `/signaller train` asks which projects and confirms once (`$.ui.ask`), then labels with `$.model.complete({model: "haiku"})`, posts labels, and shows the result. The console's Auto box gets the "Teach auto how you work" card; the plugin gets the band.

**Tech Stack:** Python 3 stdlib (router), vanilla JS/CSS under CSP (console), TypeScript hooks module (plugin).

**Spec:** `docs/superpowers/specs/2026-11-08-auto-training-design.md`

## Global Constraints

- Router: Python 3 standard library only; runs as `python3 -E -s`. Never fail closed: a scan, label or training error is a message in the card, never a dead router (AGENTS.md).
- Training and scanning never run on a request thread: one worker thread in `Trainer`.
- Prompt text is never logged, never written to `console.json`, `presets.json`, the model file, fixtures, or any GET response. It is served only by `POST /ui/api/train/next` (loopback Origin, the CSRF check every POST already has) to the labelling plugin.
- Labels live in `<config>/training/labels.jsonl`, the folder 0700 and the file 0600, deleted on install or discard.
- Label definition, verbatim from the person: "anything which isn't brainstorming, speccing or planning is execution".
- Labelling model: `haiku`. Cost estimate uses `prices.DEFAULT_ANTHROPIC["haiku"]`.
- Model format unchanged: `autophase.FORMAT` (`signaller-auto-1`), features from `autophase.features`, at most 5000 weights.
- Default cost cut-off: `5.0` USD (`auto.train_cap`), editable in the card.
- Hold-out: 20% of sessions by session id, seed 0 (`autotrain.split`, the same split as today's tool).
- "Too few to judge": fewer than 30 labelled planning prompts in all, or fewer than 5 in the held-out sessions.
- Band: lever is `auto`, no model in use, in at most the first 3 sessions on auto, not dismissed (`/signaller train hide`).
- No "/signaller" text in any prompt the plugin sends to the model.
- Plugin version → `0.11.0`.

## Review Focus

1. A transcript over 4 MiB, or 100+ MB of them in one project — the scan must stream (line by line, as `_events` does), stay off request threads, and the card shows "scanning…" rather than hanging. Test in Task 1 (a large synthetic file is still counted) and Task 3 (GET answers while a scan runs).
2. The router restarts, or the plugin reloads, mid-labelling — rerunning `/signaller train` with the same projects resumes from the labels already on disk instead of paying again. Test in Task 3.
3. Haiku answers something other than one label (empty, "Planning.", a sentence) — lenient parse; an unparseable answer is skipped and counted, never stored as a label. Test in Task 4.
4. Every labelled prompt is one class (all execution) — fit and evaluate must not divide by zero; the result says too few to judge. Test in Task 2.
5. A project folder that vanished or a transcript that can't be read between scan and labelling — skipped, counted, the run goes on. Test in Task 3.

---

### Task 1: `autotrain.py` — transcripts to labelling candidates

**Files:**
- Create: `autotrain.py`
- Modify: `tools/auto_train.py` (import `replay`, `follow_on`, `split`, `_events`, `_is_human`, `FOLLOW` from `autotrain` and delete its copies)
- Test: `tests/test_autotrain.py`

**Interfaces:**
- Produces:
  - `autotrain.replay(path) -> Iterator[tuple[list, dict]]`, `follow_on(text) -> bool`, `split(rows) -> (train, test)` — moved verbatim from `tools/auto_train.py` (rows need only `"session"`).
  - `@dataclass(frozen=True) Item: id: str  # "<session>:<idx>"; project: str; session: str; idx: int; ts: str; chars: int`
  - `autotrain.candidates(project: str, path: str) -> list[Item]` — main-thread human turns of one transcript worth labelling: drops `follow_on` texts, texts whose `routing.lead_tags` is non-empty, and texts that open with a typed command (`autophase.CMD_RE`). `idx` is the turn's position in `replay` (all turns, so eval can find it again); `ts` the human event's `timestamp`; `chars` = `min(len(text), LABEL_CHARS)`.
  - `autotrain.turn_text(path: str, idx: int) -> Optional[str]` — the human text of turn `idx` (`autophase.human_text` of the turn's last message), for serving to the labeller.
  - `autotrain.projects(claude_dir: str) -> list[dict]` — one dict per `<claude_dir>/projects/<dir>` that has at least one top-level `*.jsonl`: `{"dir", "name", "sessions", "newest"}`; `name` is the basename of the first `cwd` found (`sessions.find_transcript`-style read of the first 50 lines), else `dir`. Cheap: no replay.
  - Constant `LABEL_CHARS = 6000` (text sent per prompt; caps cost).

- [ ] **Step 1: Write failing tests** in `tests/test_autotrain.py`, building small transcripts with a helper `write_transcript(dir, sid, events)` (user events with `promptSource: "typed"`, assistant events with ids):
  - `test_candidates_skip_follow_ons_tags_and_commands`: turns "let's brainstorm a router mode", "ok", "#execution fix it", `<command-name>/superpowers:brainstorming</command-name>` → only the first is a candidate, `idx == 0`, `id == "<sid>:0"`.
  - `test_candidates_idx_matches_replay`: the 3rd human turn's candidate has `idx == 2` and `turn_text(path, 2)` returns its text.
  - `test_projects_names_from_cwd`: a project dir whose transcript carries `cwd: "/x/city-os"` → `name == "city-os"`, `sessions == 1`.
  - `test_candidates_streams_large_file`: a transcript padded past 5 MB with tool results still yields its candidates.
  - `test_tool_still_runs`: `subprocess.run([sys.executable, "tools/auto_train.py", "selftest"])` returns 0.
- [ ] **Step 2:** `pytest -q tests/test_autotrain.py` → FAIL (no module).
- [ ] **Step 3:** Create `autotrain.py` (move the replay code; `autotrain` may import `autophase`, `routing`; never the other way round). Point `tools/auto_train.py` at it.
- [ ] **Step 4:** `pytest -q tests` → all pass.
- [ ] **Step 5:** Commit `autotrain: transcripts to labelling candidates (replay moves out of the offline tool)`.

### Task 2: `autotrain.py` — estimate, fit, evaluate

**Files:**
- Modify: `autotrain.py`
- Test: `tests/test_autotrain.py`

**Interfaces:**
- Consumes: Task 1's `Item`, `replay`, `split`.
- Produces:
  - `autotrain.estimate_usd(items: list[Item]) -> float` — per item `(chars/4 + PROMPT_TOKENS) * haiku.in + OUT_TOKENS * haiku.out`, per 1M; `PROMPT_TOKENS = 300`, `OUT_TOKENS = 5`.
  - `autotrain.cut(items: list[Item], cap_usd: float) -> list[Item]` — newest sessions first (by the session's newest `ts`), whole sessions only, as many as fit under `cap_usd`.
  - `autotrain.fit(rows: list[dict]) -> dict` — rows `{text, label}`; stdlib logistic regression over `autophase.features`, class-balanced, L2, returns the model dict (`format`, `bias`, `weights`, top 5000 by |w|, zero weights dropped) that `autophase.Model` loads. Full-batch gradient descent, fixed epochs; a `ponytail:` comment names the ceiling (rows × epochs in pure Python).
  - `autotrain.evaluate(rows: list[dict], paths: dict[str, str], model: dict, s: autophase.AutoSettings) -> dict` — rows `{session, idx, label}` of the held-out sessions; `paths` session → transcript; replays each and decides with `autophase.phase(msgs, s, None)` (rules alone) and `autophase.phase(msgs, s, Model(...).score)` (with model). Returns `{"rules": {"planning_kept", "execution_moved"}, "model": {...same}, "planning", "execution", "verdict"}`; verdict `"better"` when the model keeps more planning turns and moves no fewer execution turns than it gains, `"no better"` otherwise, `"too few"` per Global Constraints.

- [ ] **Step 1: Failing tests:**
  - `test_estimate_matches_haiku_rate`: two items of 4000 chars → `round(estimate_usd(items), 6) == round(2 * ((1000 + 300) * 0.10 + 5 * 0.50) / 1e6, 6)`.
  - `test_cut_keeps_newest_whole_sessions`: three sessions, cap that fits two → the two newest, every item of each.
  - `test_fit_separates_words`: 40 rows "spec the plan for X" planning, 40 "fix the failing test in Y" execution → `Model(**)` scores a new "write the spec" ≥ 0.5 and "fix the test" < 0.5; `model["format"] == autophase.FORMAT`.
  - `test_fit_one_class_is_safe`: all execution → no exception; `evaluate` verdict `"too few"`.
  - `test_evaluate_counts`: a held-out transcript with a brainstorming skill turn then an edit turn, labels planning/execution → `rules["planning_kept"] == 1`, `rules["execution_moved"] == 1`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `pytest -q tests` → pass; time `fit` on 3000 synthetic rows and note the seconds in the commit message (spec: "seconds, to be measured").
- [ ] **Step 5:** Commit `autotrain: cost estimate, stdlib fit and held-out evaluation`.

### Task 3: The training job in the router and its API

**Files:**
- Modify: `autotrain.py` (add `Trainer`), `signaller.py` (own one, reload the model), `console.py` (endpoints, lever answer field), `routing.py` (`auto.train_cap`: `AutoSettings` field in `autophase.py`, `_auto_from_json` validation 0.5–1000, to_json), `README.md`, `AGENTS.md`
- Test: `tests/test_autotrain.py`, `tests/test_console.py`, `tests/test_routing.py`

**Interfaces:**
- Consumes: Tasks 1–2.
- Produces (all JSON; every POST needs the loopback Origin):
  - `GET /ui/api/train` → `{"state": "idle|scanning|ready|labelling|training|done|failed", "projects": [{"dir","name","sessions","prompts","usd"}], "job": {"projects","items","labelled","failed","usd_est","usd_spent","cut"} | null, "result": <evaluate dict> | null, "error": str | null, "cap": float}`. `prompts`/`usd` per project fill in as the worker scans (null until then). No text.
  - `POST /ui/api/train/plan {"projects": [dir…], "cut": bool}` → starts or resumes the job for those projects (one job at a time; a new plan replaces an unfinished one, keeping labels whose ids still occur). Returns the `job` block. `cut: true` applies `autotrain.cut` with the cap.
  - `POST /ui/api/train/next {"n": int ≤ 50}` → `{"items": [{"id", "text"}]}`: the next unlabelled items (an unreadable transcript's items are marked failed and skipped).
  - `POST /ui/api/train/labels {"labels": [{"id", "label"}], "failed": [id…], "usage": {"input_tokens", "output_tokens"}}` → appends to `labels.jsonl`, adds to `usd_spent`; returns the `job` block. Labels other than `planning|execution` → 400.
  - `POST /ui/api/train/finish` → worker splits, fits on the train part, evaluates on the held-out part; state `training` then `done` (or `failed` with `error`).
  - `POST /ui/api/train/use` → writes `<config>/auto_model.json` (`keys.write_json`, atomic), sets `app.auto_model`, deletes `training/`, state `idle`. `POST /ui/api/train/discard` → deletes `training/`, state `idle`.
  - Lever answer (`/ui/api/lever`) gains `"trained": bool` (a model is in use).

- [ ] **Step 1: Failing tests** (`tests/test_console.py`, real HTTP against a fixture app whose claude dir is a temp folder seeded with Task 1's helper):
  - `test_train_flow`: GET (wait for scan) → plan one project → next(50) returns the candidate texts → labels for all → finish → poll until `done` → `result.verdict` present → use → `GET /ui/api/state`'s `auto_model` true, `training/` gone.
  - `test_train_get_never_serves_text`: after plan, the GET body contains none of the seeded prompt strings.
  - `test_train_resume_after_restart`: label half, build a new app on the same config dir, plan the same project → `labelled` equals the half.
  - `test_train_next_needs_origin`: POST next without Origin → 403.
  - `test_train_unreadable_transcript`: delete a transcript after plan → next skips it, `failed` counts its items.
  - `test_train_bad_label_400`.
  - `test_lever_answer_trained_flag`.
  - `tests/test_routing.py::test_auto_train_cap_roundtrip_and_bounds`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement. Scanning `prompts` per project runs `candidates` over every transcript in the worker, caching per `(path, mtime, size)`; the first GET starts it.
- [ ] **Step 4:** `pytest -q tests` → pass.
- [ ] **Step 5:** Docs: README Auto mode gets "Teach auto how you work" (flow, cost, where labels live, that the Docker `~/.claude/projects` mount is now read for prompt text, not only session labels); AGENTS.md: the text-only-via-POST-next rule and `training/` cleanup.
- [ ] **Step 6:** Commit `Auto training: the router's job, its API and model reload`.

### Task 4: Plugin — `/signaller train`, labelling, result, band

**Files:**
- Modify: `plugin/signaller/hooks/register.ts`, `plugin/signaller/.claude-plugin/plugin.json` (0.11.0, description mentions train)
- Test: `plugin/signaller/tests/gate.test.ts`

**Interfaces:**
- Consumes: Task 3's endpoints and the lever answer's `trained`.
- Produces:
  - `/signaller train` (checked before the settings form, like `run`): GET `/ui/api/train`; `$.ui.ask` multi-select of up to 4 projects with the most prompts ("name · N prompts · $x"), Other = free text dirs; plan; `$.ui.ask` confirm: "Label N prompts (about $x)?" with options `Label all`, `Newest only (about $y)` (only when over the cap), `Cancel`. `/signaller train <dir…>` skips the first question. Then returns at once ("labelling N prompts…") and runs the loop unawaited.
  - The loop: `next(20)` → `$.model.complete({model: 'haiku', system: LABEL_SYSTEM, prompt: text, maxTokens: 5})` for each, 4 at a time; parse = lowercased reply contains exactly one of `planning`/`execution`, else failed; post labels with summed usage; `$.ui.status` shows `training auto · k/N`; repeat until empty; `finish`; poll GET every 2 s until `done|failed`; `$.ui.log` the result line ("Your model keeps P of Q planning turns on Anthropic (rules alone: R); moves E of F execution turns."); then `$.ui.ask` "Use your model?" `Use it` / `Keep rules only` (Keep first when verdict isn't `better`) → POST use/discard.
  - `LABEL_SYSTEM`: the definition verbatim plus "Answer with one word: planning or execution." — no "/signaller" in it.
  - Band: `ui.render AbovePrompt` when the lever answer says `lever === 'auto' && !trained`, the session is among the first 3 recorded in `$.store` key `trainHint` (`{sessions: string[], hidden: boolean}`), and not hidden: "auto runs on rules only · /signaller train teaches it your habits (about $x) · /signaller train hide". The image-fallback band wins when both apply.
  - GRANT: `train` joins `run|name` as an exempt word.

- [ ] **Step 1: Failing tests:**
  - `train labels each pending prompt with haiku and posts the labels` (stub `model.complete` returns "Planning." then "execution"; `ui.ask` answers; assert the labels POST body and that no request to the model contains "/signaller").
  - `an unparseable reply is sent as failed, not as a label`.
  - `quoting /signaller train keeps the queue`.
  - `the band shows in the first 3 auto sessions only, and not after hide`.
- [ ] **Step 2:** `claude plugin test plugin/signaller` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** `claude plugin test plugin/signaller` → all pass.
- [ ] **Step 5:** Commit `Plugin 0.11.0: /signaller train labels with Haiku; band while auto is untrained`.

### Task 5: Console — the "Teach auto how you work" card

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/app.css` (inside the existing Auto box)

**Interfaces:**
- Consumes: `GET /ui/api/train` and `POST …/plan|use|discard`; `auto.train_cap` via the existing config save.

- Card states, from `GET /ui/api/train` on the page's existing refresh:
  - **idle/ready:** projects with checkboxes (name, sessions, prompts or "scanning…", cost); total prompts and cost for the ticked set; when over the cap, "Newest sessions only: N prompts, about $y" note; the cap field; the command `/signaller train <dirs>` with a Copy button (clipboard API, fallback select-all).
  - **labelling/training:** progress bar `labelled/items`, spent so far.
  - **done:** the result card: two rows (planning kept on Anthropic, execution moved to the downstream), "your model" against "rules alone"; buttons **Use it** / **Keep rules only**, the primary one per verdict; "too few" shows the text "Not enough planning prompts to judge yet" and only Keep.
  - **failed:** the error, and a Try again that re-plans.
- Reuse `el`, `api`; no inline styles (CSP); colours via `style.setProperty`.

- [ ] **Step 1:** Implement.
- [ ] **Step 2:** `node --check ui/app.js` → ok; `pytest -q tests/test_console.py::test_page_assets_local_only` → pass.
- [ ] **Step 3:** Browser check on a scratch router (port 8788, scratch `SIGNALLER_CONFIG_DIR`, `CLAUDE_CONFIG_DIR` pointing at a scratch folder seeded with synthetic transcripts — never real ones): tick projects, see totals, copy the command; drive the API with curl through labels/finish to see the result card; click Use it. Do not view images in subagents: check through the page snapshot.
- [ ] **Step 4:** Commit `Console: Teach auto how you work`.
