# Sessions & Cost Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tag every request with its Claude Code session, record tokens and cost per request, and let the console filter everything by session(s) and time span, with per-downstream cost and Anthropic-savings cards. Valar requests carry a per-slot completion window.

**Architecture:** Recording grows in the proxy (`claude_router.py`): session id from the request, usage from the response via the existing `AnswerCapture`, cost from a new `prices.py`. A new `sessions.py` resolves session labels off the request thread. `store.py` gains columns, a `sessions` table and filtered read queries; `console.py` passes `sessions`/`from`/`to` through; the page adds chips, a time control and cost cards in the existing theme.

**Tech Stack:** Python 3 standard library only, `unittest`; vanilla JS/CSS in `ui/`.

**Spec:** `docs/superpowers/specs/2026-10-29-sessions-cost-design.md`. Mockups: `docs/superpowers/specs/2026-10-29-sessions-cost/sessions-page-v3.html` (whole page) and `timeframe-v4.html` (option A).

## Global Constraints

- Standard library only; the page loads nothing from the internet.
- Never fail or delay a proxied request: session lookup, usage parsing, pricing and store writes catch everything; on error the row records nulls. Usage is parsed only after bytes are written to the client.
- Read only `cwd` and `gitBranch` from transcript lines (max 50 lines per file).
- Look: existing tokens and components only (white cards with coloured left border, Barlow Condensed numbers, `.seg` segmented control, chips styled like levers with ink background when selected). Match the mockups.
- Valar window header: `X-Valar-Completion-Window: asap|priority|standard`. `flex` and `batch` are never sent. Valar price tier names map `realtime→asap`, `priority→priority`, `standard→standard`.
- Valar cache writes cost 0 (non-Claude models). Anthropic cache write = 1.25 × input.
- Anthropic API prices seeded into `prices.json` (USD per 1M tokens; in / out / cache read / cache write):
  - fable `10 / 50 / 0.25 / 12.5`
  - opus `4 / 20 / 0.20 / 5`
  - sonnet `2 / 10 / 0.20 / 2.5`
  - haiku `0.10 / 0.50 / 0.01 / 0.125` (haiku cache read assumed 0.1× input; the >100K-prompt rate is ignored, see Task 4 note)
  - `other` tier → sonnet's row.
- Time spans: `15 min`, `1 hour` (default on first load), `24 h`, `Whole session`, `Custom…`. Departures and side by side ignore the span.
- Tests: `python3 -m unittest` from repo root; the whole suite stays green after every task, including `test_speed`.

## Review Focus

1. **A non-Claude-Code client** (curl, no session header, no `metadata.user_id`) → row has `session_id` NULL; it shows as "unknown session" and is counted under "All". Test in Task 1.
2. **Usage missing or unparseable** (gzip body, error response, truncated stream, upstream 500) → token columns NULL, cost NULL, request bytes unchanged. Test in Task 1.
3. **Transcript not there yet / unreadable / `CLAUDE_CONFIG_DIR` set** → label falls back to the first 8 chars of the id, lookup retried ≤ every 30 s, never raises. Test in Task 2.
4. **Valar prices not loaded yet, or a model with no price** (`valar-auto-balanced`) → `cost` NULL; repriced once prices arrive (priced models only); summary reports `unpriced` count. Test in Task 4.
5. **A saved `console.json` from before this change** (no `windows`, no servertool rule) and **`stats.db` rows from before this change** (NULL new columns) load and sum without errors (`COALESCE`). Tests in Tasks 1 and 3.

---

### Task 1: Session id and usage on every recorded request

**Files:**
- Modify: `store.py` (schema + migration), `doubling.py` (`AnswerCapture`), `routing.py` (`session_of`), `claude_router.py` (handler)
- Test: `tests/test_store.py`, `tests/test_doubling.py`, `tests/test_proxy.py`, `tests/test_routing.py`

**Interfaces:**
- Produces:
  - `routing.session_of(headers: Mapping[str, str], req: dict | None) -> str | None`: header `X-Claude-Code-Session-Id` (case-insensitive) first, else `json.loads(req["metadata"]["user_id"])["session_id"]`, else None. Never raises.
  - `AnswerCapture.result()` gains `"usage": {"in": int|None, "out": int|None, "cache_read": int|None, "cache_write": int|None}`. SSE: `message_start.message.usage` gives in/cache_read/cache_write (and out); `message_delta.usage.output_tokens` overrides out. JSON: `obj["usage"]`.
  - `requests` columns added: `session_id TEXT, in_tok INTEGER, out_tok INTEGER, cache_read_tok INTEGER, cache_write_tok INTEGER, window TEXT, cost REAL, api_cost REAL`. `turns` gains `session_id TEXT`. `pairs` gains `shadow_cost REAL`. Index `requests_sess_ts ON requests(session_id, ts)`.
  - `Store._migrate(conn)`: `PRAGMA table_info` per table, `ALTER TABLE … ADD COLUMN` for each missing column; called from `Store.__init__` after `SCHEMA`.

- [ ] **Step 0: Confirm subagents send the parent's id on the wire.** Run a logging pass-through in front of the router (forward to it, log `X-Claude-Code-Session-Id` per request), then `claude -p "Use the Task tool to have a subagent list the files here, then stop." --max-turns 4` through it. Expected: every request carries one id. If subagent requests carry another id, add to Task 2 a mapping via `<claude dir>/projects/*/<parent>/subagents/agent-<id>.jsonl` (folder name = parent) and tell the owner.
- [ ] **Step 1: Failing tests.**
  - `test_routing.SessionTests.test_session_of`: header wins over metadata; metadata JSON string works; bad JSON / missing / non-dict → None.
  - `test_doubling.CaptureTests.test_usage_sse_and_json`: SSE with `message_start` usage `{input_tokens: 10, cache_read_input_tokens: 5, cache_creation_input_tokens: 2, output_tokens: 1}` and `message_delta` `{output_tokens: 7}` → `{"in": 10, "out": 7, "cache_read": 5, "cache_write": 2}`; JSON body → same from `usage`; garbage → all None.
  - `test_store.MigrationTests.test_old_db_gets_columns`: create a db with the old `requests`/`turns`/`pairs` schema and one row, open `Store` → new columns exist, old row readable with NULLs.
  - `test_proxy`: a streamed `/v1/messages` with header `X-Claude-Code-Session-Id: S1` through the fake upstream (emit usage in `message_start`/`message_delta`) → stored row has `session_id == "S1"`, `in_tok`, `out_tok` set; a request with no id → `session_id` None; fake upstream returning 500 → token columns None, client still gets the 500 body unchanged.
- [ ] **Step 2: Run them, expect failures** (`python3 -m unittest tests.test_routing tests.test_doubling tests.test_store tests.test_proxy`).
- [ ] **Step 3: Implement.** In the handler: compute `sid = session_of(self.headers, req)` once; create `AnswerCapture` whenever `rec` (not only when `pair`), feed after write as today; in `finally` add `session_id` and `in_tok/out_tok/cache_read_tok/cache_write_tok` from `cap.result()["usage"]` to the `record_request` row. `Doubler.turn_for` gets a `session_id` kwarg and stores it on the `turns` row. Wrap usage extraction in try/except → None.
- [ ] **Step 4: Full suite green** (`python3 -m unittest`).
- [ ] **Step 5: Commit** `feat: record session id and token usage per request`.

### Task 2: Session labels from Claude Code transcripts

**Files:**
- Create: `sessions.py`
- Modify: `store.py` (sessions table + `touch_session`), `claude_router.py` (`App` wiring)
- Test: `tests/test_sessions.py`

**Interfaces:**
- Consumes: `session_of` (Task 1).
- Produces:
  - `sessions` table: `id TEXT PRIMARY KEY, cwd TEXT, branch TEXT, first_ts REAL, last_ts REAL`. Pruned with requests (`last_ts < now - RETAIN`).
  - `Store.touch_session(id: str, ts: float, cwd: str | None = None, branch: str | None = None) -> None`: queued write; upsert keeps `first_ts = MIN`, `last_ts = MAX`, `cwd/branch = COALESCE(new, old)`.
  - `sessions.claude_dir(env) -> str`: `env["CLAUDE_CONFIG_DIR"]` or `~/.claude`, expanded.
  - `sessions.find_transcript(claude_dir: str, sid: str) -> tuple[str | None, str | None]`: glob `projects/*/<sid>.jsonl`, read ≤ 50 lines, return `(cwd, gitBranch)` from the first line(s) having them; `(None, None)` if absent/unreadable. Never raises.
  - `sessions.label(sid: str | None, cwd: str | None, branch: str | None) -> str`: `"<basename(cwd)> · <branch>"`, branch omitted when empty; no cwd → `sid[:8]`; no sid → `"unknown session"`.
  - `class SessionDirectory(store, claude_dir, clock=time.time)`: `note(sid, ts)` (called per recorded request, non-blocking) → `store.touch_session(sid, ts)`; when the sid's cwd is unknown and the last attempt was ≥ 30 s ago (first time: immediately), hands the sid to one daemon worker thread that calls `find_transcript` and, on success, `store.touch_session(sid, ts, cwd, branch)`.
- [ ] **Step 1: Failing tests** in `tests/test_sessions.py`: temp claude dir with `projects/-tmp-x/<sid>.jsonl` whose 3rd line has `"cwd":"/tmp/x","gitBranch":"main"` → `("/tmp/x", "main")`; missing file → `(None, None)`; a directory named `<sid>.jsonl` → `(None, None)`; `label` cases (cwd+branch, no branch, no cwd, None sid); `SessionDirectory` with a fake clock: first `note` looks up, a second `note` 10 s later with the file still missing does not, one at 31 s does; `CLAUDE_CONFIG_DIR` respected by `claude_dir`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** `sessions.py`; `App.__init__` builds `self.sessions = SessionDirectory(self.store, claude_dir(os.environ))`; the handler calls `app.sessions.note(sid, t0)` when `rec and sid`, inside a try.
- [ ] **Step 4: Full suite green.**
- [ ] **Step 5: Commit** `feat: label sessions from Claude Code transcripts`.

### Task 3: Valar completion window per model slot

**Files:**
- Modify: `routing.py` (config `windows`, `track_window`), `claude_router.py` (header on served Valar requests), `doubling.py` (header on shadow Valar requests), `ui/index.html`, `ui/app.js`, `ui/app.css`
- Test: `tests/test_routing.py`, `tests/test_proxy.py`, `tests/test_doubling.py`

**Interfaces:**
- Consumes: `MODEL_TIERS`, `tier_of` (routing.py).
- Produces:
  - `RoutingConfig.windows: dict` — `{"valar": {tier: "asap"|"priority"|"standard" for tier in MODEL_TIERS}}`; JSON key `windows`; missing in saved JSON → all `"asap"`; any other value → `ValueError("windows.valar.<tier> must be asap, priority or standard")`.
  - `routing.track_window(model_in: str, windows: dict) -> str`: `windows.get(tier_of(model_in), windows["sonnet"])`; non-`claude-` model ids → `windows["sonnet"]`.
  - Recorded `window` column: the window sent (Valar rows only; NULL otherwise).
- [ ] **Step 1: Failing tests:** config without `windows` loads as all `asap` and round-trips with `windows`; bad value raises the message above; `track_window` per tier; proxy: Valar-served request carries `X-Valar-Completion-Window: standard` when the sonnet slot is `standard`, and the row's `window == "standard"`; Anthropic- and local-served requests carry no such header; shadow to Valar carries the header for its slot.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.** Header added via `upstream_headers` call sites (served in the handler, shadow in `Doubler._run`). The classifier's Valar calls send no header (Now).
- [ ] **Step 4: Page.** Under each Valar slot's model dropdown, a small `.seg` with `Now · Priority · Standard` (Local row: none). Offer only windows that model has a price row for (from `/ui/api/models`, Task 4 adds `windows` per model to that response; until then show all three). Changing the model to one lacking the slot's window saves the slot as `asap`. Saves via the existing `save({...cfg, windows: …})`.
- [ ] **Step 5: Full suite green; commit** `feat: per-slot Valar completion window`.

### Task 4: Prices and cost at record time

**Files:**
- Create: `prices.py`
- Modify: `console.py` (`list_models` keeps pricing; `/ui/api/models` returns per-model windows), `claude_router.py` (App: price refresh thread; cost on record), `doubling.py` (shadow usage + cost on pair), `store.py` (`reprice`)
- Test: `tests/test_prices.py`, `tests/test_proxy.py`, `tests/test_console.py`

**Interfaces:**
- Consumes: usage dict (Task 1), `window` (Task 3), `tier_of`.
- Produces:
  - `models.json` gains `"valar_prices": {model_id: {window: [in, cached_in, out]}}` (per 1M; windows from `realtime/priority/standard`, mapped per Global Constraints). Existing `"valar"`/`"local"` id lists unchanged.
  - `prices.parse_valar(models_payload: dict) -> dict` (the shape above; models with empty `pricing` omitted).
  - `prices.DEFAULT_ANTHROPIC: dict[tier, dict[str, float]]` with keys `in, out, cache_read, cache_write` (Global Constraints values; `other` → sonnet).
  - `class Prices(config_dir)`: `load()` reads `models.json["valar_prices"]` and `prices.json` (written with `DEFAULT_ANTHROPIC` if missing; unreadable → defaults + stderr note); `set_valar(prices: dict)`; `cost(track: str, model_out: str, window: str | None, usage: dict) -> float | None` (anthropic → 0.0; local → 0.0; valar → priced or None; usage all None → None); `api_cost(model_in: str, usage: dict) -> float | None`.
  - `App` starts a daemon thread: `refresh_valar_prices()` at start and every 3600 s — fetch `/v1/models` with the Valar key (reuse `console.list_models`' no-redirect opener, extended to return the raw payload), `Prices.set_valar`, write `models.json`, then `store.reprice(prices)`.
  - `Store.reprice(prices) -> int`: for Valar rows with any token column set and `cost IS NULL`, compute and update `cost`; returns rows updated.
  - Shadow side JSON gains `"usage"` and `"cost"`; `pairs.shadow_cost` set from it.
- [ ] **Step 1: Failing tests** (`tests/test_prices.py`): `parse_valar` on the probe payload shape (Kimi-K3 realtime `2.25/0.225/11.25`) → `{"moonshotai/Kimi-K3": {"asap": [2.25, 0.225, 11.25], …}}`; `valar-auto-balanced` omitted; `cost("valar","moonshotai/Kimi-K3","standard",{"in":1_000_000,"out":0,"cache_read":0,"cache_write":500})` → `1.125` (cache write free); unknown model → None; `api_cost("claude-opus-5-5", {"in":1e6,"out":1e6,"cache_read":1e6,"cache_write":1e6})` → `4+20+0.2+5 = 29.2`; `api_cost` for `claude-3-5-foo` uses sonnet's row; `prices.json` missing → written with defaults; a hand-edited `prices.json` is honoured. Proxy: a Valar-served streamed request stores `cost` and `api_cost`; Anthropic-served stores `cost == 0` and `api_cost` > 0; with no Valar prices loaded `cost` is NULL and `reprice` fills it after `set_valar`. Console: `/ui/api/models?track=valar` includes `"windows": {model: ["asap", …]}`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement.** Note in `prices.py` with a `ponytail:` comment: Haiku's >100K-prompt rate is ignored (estimate only).
- [ ] **Step 4: Full suite green; commit** `feat: price requests and comparisons at record time`.

### Task 5: Filtered reads and the API

**Files:**
- Modify: `store.py`, `console.py`
- Test: `tests/test_store.py`, `tests/test_console.py`

**Interfaces:**
- Consumes: columns from Tasks 1–4.
- Produces:
  - Filter arguments everywhere: `sessions: list[str] | None` (None = all; the literal `"unknown"` matches `session_id IS NULL`), `t0: float`, `t1: float`.
  - `Store.summary(sessions, t0, t1, now) -> dict`:
    `{"total": n, "per_minute": float (last 5 min within the filter),
      "tracks": {t: {"n", "pct", "in", "out", "cost", "api_cost", "unpriced", "comparisons"}},
      "savings": float (Σ valar+local served rows of api_cost − COALESCE(cost,0)),
      "rules": {"<rule name>"|"classifier"|"otherwise"|"lever": n},
      "errors": n, "tally": {anthropic, valar, local, same}}`.
    `comparisons[t]` = Σ `pairs.shadow_cost` where `shadow_track = t`, joined to `requests` on `request_id` for session/time. Rule key from `reason`: `rule:<name>[:n]` → `<name>`; `about:*` → `"classifier"`; `default` → `"otherwise"`; `lever` → `"lever"`; `passthrough` skipped. `tally` counts verdicts of turns in the filter.
  - `Store.list_sessions(t0, t1) -> list[dict]`: `{id, label, cwd, branch, first_ts, last_ts, n}` for sessions with ≥1 request in span, ordered by last request desc; plus one `{"id": "unknown", "label": "unknown session", …}` entry if such rows exist. Label via `sessions.label`.
  - `Store.departures(since_id, before_id, limit, sessions=None)` and `Store.turns(unjudged_first, limit, sessions=None)` filter by session.
  - "Whole session" `t0` = `MIN(first_ts)` over the picked sessions (all: oldest kept request) — computed by the API when `from=session`.
  - Console: `/ui/api/state?sessions=a,b&from=<ts|session>&to=<ts>` returns today's fields plus `"summary"` (stats keep their existing keys for the unfiltered header); `/ui/api/departures` and `/ui/api/turns` accept `sessions`; new `GET /ui/api/sessions?from&to`. Bad `from`/`to` → 400 `"from/to must be numbers or 'session'"`.
- [ ] **Step 1: Failing tests:** seed a store with rows across two sessions, three tracks, two time ranges, one unknown-session row, one pre-migration row (NULL tokens), one Valar row with `cost` NULL, one pair with `shadow_cost`; assert `summary` per-track counts/pct/cost/api_cost/unpriced/comparisons, `savings`, `rules` keys, and that the filter by one session and by span changes them as expected; `list_sessions` order and the `unknown` entry; departures/turns filtered; console 400 on `from=abc`.
- [ ] **Step 2: Run, expect failures.**
- [ ] **Step 3: Implement** with `COALESCE` on every summed column; one SQL pass per summary part.
- [ ] **Step 4: Full suite green incl. `test_speed`** (add a `summary` call over 10k rows < 50 ms to `test_speed`).
- [ ] **Step 5: Commit** `feat: session and time filters for stats, departures and turns`.

### Task 6: The page

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/app.css`
- Test: manual against a throwaway router (below); `node --check ui/app.js`

**Interfaces:**
- Consumes: API from Task 5, windows from Task 3.

- [ ] **Step 1: Header strip** (mockup v4 A): `Showing` chips — `All N sessions`, then sessions from `/ui/api/sessions` (8 most recent, then `+ N more` opening the full list); click toggles; `All` clears. Time `.seg` beside them: `15 min · 1 hour · 24 h · Whole session · Custom…`; Custom shows two `<input type="datetime-local">`. Pick + span saved in `localStorage` (try/catch); remembered sessions not in the list are dropped.
- [ ] **Step 2: Yard:** percentages and req/min from `summary`; dots only for departures in the picked sessions; caption `share of the last hour` / `last 15 min` / `last 24 h` / `whole session` / `HH:MM–HH:MM`; `Claude Code · N sessions` when filtered.
- [ ] **Step 3: Cost cards** under the yard (mockup v3): Anthropic `$0` + `$X at API prices · in / out`; Valar cost + tokens, `partly unpriced` when `unpriced > 0`, `incl. $Y comparisons` when `comparisons > 0`; Local `$0`; dark savings card `≈ $S` + `Valar + local would have cost $T`. Card label gets `· last hour` etc. unless span is Whole session.
- [ ] **Step 4: Rulebook:** grey `Rules for all sessions` line (no link); each rule and Otherwise shows `decided N of M` from `summary.rules`.
- [ ] **Step 5: Departures and side by side:** pass `sessions` to their endpoints; each departure row gets a `.stag` with the session's short label; an opened departure adds `session · model · in / out · $cost (would be $api_cost on Anthropic)`; side-by-side cards show each side's cost; tally counts follow the filter.
- [ ] **Step 6: Check in a browser:** run a throwaway router (`CLAUDE_ROUTER_CONFIG_DIR=<tmp> CLAUDE_ROUTER_LISTEN=127.0.0.1:8799`) seeded with a copy of `stats.db` and `console.json`, open `/ui/`, compare against both mockups at desktop width and at 390 px; pick 1 and 2 sessions and each span; confirm no console errors.
- [ ] **Step 7: Commit** `feat: session chips, time span and cost cards in the console`.

### Task 7: Docs and live acceptance

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** README: sessions & cost section (where labels come from, `prices.json`, Valar windows and what they cost in latency, that history before this release has no session/cost).
- [ ] **Step 2:** Owner restarts the live router. Run two Claude Code sessions in different folders through it for a few requests each (one doing a WebSearch). Check: two chips with the right folder · branch; filters move every section; Valar cost is non-zero and matches tokens × price; Anthropic shows API-price figure; savings card plausible; departures show session tags.
- [ ] **Step 3:** Commit `docs: sessions and cost`.
