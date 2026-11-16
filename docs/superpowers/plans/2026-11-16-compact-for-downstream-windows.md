# Compact for Downstream Windows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When a request to a downstream would come within 10% of that model's context window, signaller answers "prompt is too long" itself, so Claude Code compacts on the same downstream and retries.

**Architecture:** A new pure module `compact.py` holds the window parsing, the per-conversation size estimate and the refusal body. The router reads windows from the downstream's config (manual, console.json v6) or from `models.json` (`listed_context`, fetched from `/v1/models` or LiteLLM's `/model/info`), checks each downstream-bound `/v1/messages` request before the shadow and the upstream call, and answers 400 when over. The console row shows and edits the window per model.

**Tech Stack:** Python 3 stdlib only (router), vanilla JS/CSS (console), unittest/pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-compact-for-downstream-windows-design.md`

## Global Constraints

- Router: Python 3 standard library only; runs as `python3 -E -s`.
- Never fail closed: any exception in window lookup, estimate or check → the request is sent as today.
- Never log or store request bodies, prompts, headers or keys; a log line stays `route= reason= model= status= ms= stream= [err=] shadow=`.
- Trigger: 90% of the window, fixed, in one constant with a `ponytail:` comment naming the limit.
- Refusal body, exactly: `{"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long: <estimate> tokens > <limit> maximum"}}`, status 400.
- Compaction requests (`x-claude-code-request-class: compaction`) are never refused.
- Anthropic-bound requests and doubling shadows are never checked.
- Manual windows: integers 1000–10000000, keyed by model id.
- console.json goes to version 6; a v5 config migrates with `context: None`.
- Work on branch `worktree-compact-windows` (the current worktree); commit per task; push only when the user asks.
- Tests: `pytest -q tests` green (use `python3 -m pytest` or a venv with pytest; the suite imports pytest); `node --check ui/app.js`.

## Review Focus

1. A downstream whose `/v1/models` lists no windows and has no `/model/info` (plain OpenAI server, 404): no check, no error, no banner. → Task 2 test `test_windows_none_listed`.
2. A session right after a compaction (`x-claude-code-context-compacted`): the old anchor must not refuse the much smaller retry. → Task 1 test `test_anchor_dropped_after_compaction` and Task 4 test.
3. A refused request in a doubled turn: no shadow request leaves. → Task 4 test `test_refused_has_no_shadow`.
4. A refresh that returns a payload with no windows must not wipe saved ones. → Task 2 test `test_empty_payload_keeps_saved_windows`.
5. Two tiers on the same model: editing one tier's window changes the other (per model id). → Task 5 browser check.

---

### Task 1: `compact.py` — windows, estimate, refusal (pure)

**Files:**
- Create: `compact.py`
- Test: `tests/test_compact.py`

**Interfaces:**
- Produces:
  - `LIMIT = 0.9`
  - `listed_windows(payload: dict) -> dict[str, int]` — from a `/v1/models` payload: per `data[].id`, the first int > 0 of `context_length`, `max_input_tokens`, `max_model_len`. Models with none are omitted.
  - `model_info_windows(payload: dict) -> dict[str, int]` — from LiteLLM `/model/info`: `data[].model_name` → `model_info.max_input_tokens` (int > 0), smallest when a name repeats.
  - `window_for(ds, model: str, listed: dict) -> int | None` — `ds.context[model]` if set, else `listed.get(ds.id, {}).get(model)`, else None.
  - `class Anchors` (in-memory, `autophase.BoundedDict(4096)` inside, thread-safe with a lock): `note(key, body_len: int, total: int) -> None`, `drop(key) -> None`, `estimate(key, body_len: int) -> int`.
  - `over(estimate: int, window: int) -> int | None` — the limit `int(window * LIMIT)` when `estimate >= limit`, else None.
  - `error_body(estimate: int, limit: int) -> bytes` — the exact refusal JSON from Global Constraints.

- [ ] **Step 1: Write the failing tests** in `tests/test_compact.py`:

```python
def test_listed_windows(self):
    p = {"data": [{"id": "a", "context_length": 850000}, {"id": "b", "max_input_tokens": 200000},
                  {"id": "c", "max_model_len": 131072}, {"id": "d"}, {"id": "e", "context_length": 0}]}
    self.assertEqual(listed_windows(p), {"a": 850000, "b": 200000, "c": 131072})
    self.assertEqual(listed_windows({}), {})

def test_model_info_windows(self):
    p = {"data": [{"model_name": "ds", "model_info": {"max_input_tokens": 900000}},
                  {"model_name": "ds", "model_info": {"max_input_tokens": 850000}},
                  {"model_name": "x", "model_info": {"max_input_tokens": None}}]}
    self.assertEqual(model_info_windows(p), {"ds": 850000})

def test_window_for(self):
    ds = replace(DEFAULT_CONFIG.downstreams[0], context={"m": 500000})
    listed = {"valar": {"m": 850000, "n": 128000}}
    self.assertEqual((window_for(ds, "m", listed), window_for(ds, "n", listed), window_for(ds, "z", listed)), (500000, 128000, None))

def test_estimate(self):
    a = Anchors()
    self.assertEqual(a.estimate("k", 4000), 1000)            # no anchor: bytes/4
    a.note("k", 40000, 9000)
    self.assertEqual(a.estimate("k", 48000), 11000)          # anchor + growth/4
    self.assertEqual(a.estimate("k", 30000), 9000)           # shrink never goes below the anchor's figure

def test_anchor_dropped_after_compaction(self):
    a = Anchors(); a.note("k", 4_000_000, 800_000); a.drop("k")
    self.assertEqual(a.estimate("k", 400_000), 100_000)

def test_over_and_body(self):
    self.assertEqual((over(764_999, 850_000), over(765_000, 850_000)), (None, 765000))
    self.assertEqual(json.loads(error_body(780000, 765000)), {"type": "error", "error": {"type": "invalid_request_error",
                     "message": "prompt is too long: 780000 tokens > 765000 maximum"}})
```

(`Downstream.context` arrives in Task 3; until then `test_window_for` builds a `SimpleNamespace(id="valar", context={"m": 500000})` instead of `replace(...)` — switch to `replace` in Task 3.)

- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_compact.py` — expect FAIL (`No module named 'compact'`).
- [ ] **Step 3: Implement `compact.py`** to the Interfaces above. `estimate`: `total + max(0, body_len - anchor_len) // 4` with an anchor, else `body_len // 4`. Malformed payload entries are skipped, never raised.
- [ ] **Step 4: Run** `python3 -m pytest -q tests/test_compact.py` — expect PASS.
- [ ] **Step 5: Commit** `git add compact.py tests/test_compact.py && git commit -m "compact: windows from model lists, the size estimate, the refusal body"`

### Task 2: Listed windows — fetch, save, serve

**Files:**
- Modify: `console.py` (`fetch_models` area, `probe`, `add_downstream`, the `GET /ui/api/models` branch)
- Modify: `signaller.py` (`App.__init__`, `refresh_prices`)
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: `compact.listed_windows`, `compact.model_info_windows`.
- Produces:
  - `console.fetch_windows(url: str, key: str, payload: dict | None) -> dict[str, int]` — `listed_windows(payload)`; if empty, GET `url + "/model/info"` (same no-redirect opener and bearer key as `fetch_models`) → `model_info_windows`; any error → `{}`.
  - `App.listed_context: dict[str, dict[str, int]]` — `{downstream id: {model: tokens}}`, loaded from `models.json` key `listed_context` at start.
  - `App.set_listed_context(id: str, windows: dict) -> None` — no-op when `windows` is empty; else updates memory and `save_models(listed_context=...)` (OSError ignored).
  - `GET /ui/api/models` adds `"context": {model: tokens}` for that track (live, or the saved one on the `stale` path).

- [ ] **Step 1: Write the failing tests** in `tests/test_console.py`, using the existing fake downstream (`self.l`) and extending it to answer `/model/info` when set:
  - `test_models_returns_listed_context`: fake `/v1/models` lists `{"id": "deepseek-v4.1-flash", "context_length": 850000}` → `GET /ui/api/models?track=lemmi` has `context == {"deepseek-v4.1-flash": 850000}`, and `models.json` has `listed_context.lemmi`.
  - `test_model_info_fallback`: `/v1/models` lists ids only; `/model/info` returns `{"data":[{"model_name":"deepseek-v4.1-flash","model_info":{"max_input_tokens":850000}}]}` → same `context`.
  - `test_windows_none_listed`: ids only and `/model/info` 404 → `context == {}`, status 200, no banner in `state`.
  - `test_empty_payload_keeps_saved_windows`: first fetch saves 850000; change the fake to list ids only and 404 `/model/info`; fetch again → `context` still `{"deepseek-v4.1-flash": 850000}`.
  - `test_add_downstream_captures_windows`: adding via `POST /ui/api/downstreams` with the fake listing a window → `app.listed_context["lemmi"]` set without any models call.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_console.py -k "context or windows or model_info"` — expect FAIL.
- [ ] **Step 3: Implement** `fetch_windows`, `App.listed_context` / `set_listed_context`, the models-endpoint field (return saved `listed_context[t]` when nothing new was found, so the row always sees the effective listed set), window capture in `add_downstream` (reuse the probe's payload — make `probe` return it under a private key the endpoint drops, or re-fetch once; builder's call), and in `refresh_prices` fetch windows for **every** keyed downstream (prices stay Valar-only).
- [ ] **Step 4: Run** `python3 -m pytest -q tests` — expect all PASS.
- [ ] **Step 5: Commit** `git commit -am "downstreams: read each model's context window from /v1/models or /model/info"` (add new files if any).

### Task 3: Manual windows — `Downstream.context`, console.json v6

**Files:**
- Modify: `routing.py` (`Downstream`, `_downstream`, `config_from_json`, `config_to_json`)
- Create: `tests/fixtures/console_v5.json` (today's `config_to_json(DEFAULT_CONFIG)` output, version 5, no `context`)
- Test: `tests/test_routing.py`, `tests/test_compact.py` (switch `test_window_for` to `replace`)

**Interfaces:**
- Produces: `Downstream.context: dict | None = None` (last field, after `advisor`); config version 6.

- [ ] **Step 1: Write the failing tests:**
  - `test_v5_migrates_with_no_context`: `config_from_json(load("console_v5.json")).downstreams[0].context is None`, and `config_to_json(...)["version"] == 6`.
  - `test_context_validated`: `context={"m": 850000}` round-trips; `{"m": 999}`, `{"m": 10_000_001}`, `{"m": "x"}`, `{"": 5000}` and a non-dict each raise `ValueError` naming the downstream.
  - Update the existing v4 test at `tests/test_routing.py:726` to expect version 6 and `context: None` on each downstream.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_routing.py` — expect FAIL.
- [ ] **Step 3: Implement**: v5 → v6 step in `config_from_json` (`{**d, "context": None}` per downstream), the accepted version becomes 6, `_downstream` validates `context` (None or `{non-empty str: int 1000..10_000_000}`; `{}` stored as None), `config_to_json` writes version 6 (`asdict` carries `context`). `AGENTS.md`: change "version 5 (v5 added …)" to "version 6 (v6 added each downstream's per-model `context` windows)".
- [ ] **Step 4: Run** `python3 -m pytest -q tests` — expect all PASS (fix any other test that pins version 5 to 6).
- [ ] **Step 5: Commit** `git commit -am "config v6: a manual context window per model on each downstream"` (with the new fixture added).

### Task 4: The check in the request path

**Files:**
- Modify: `signaller.py` (`App.__init__`, the `/v1/messages` handler between routing and the doubling block; the response `finally`; count_tokens response read)
- Test: `tests/test_console.py` (real HTTP against the fixture app, fake downstream)

**Interfaces:**
- Consumes: `compact.Anchors`, `compact.window_for`, `compact.over`, `compact.error_body`, `App.listed_context`, `Downstream.context`.
- Produces: `App.anchors: compact.Anchors`.

Order in the handler (served request only, `route != "anthropic"`, path `/v1/messages`, not labelling, header `x-claude-code-request-class` != `compaction`):
1. key = `(sid, self.headers.get("x-claude-code-agent-id") or "main")`; skip the check when `sid` is None.
2. If header `x-claude-code-context-compacted` is present: `app.anchors.drop(key)`.
3. The sent model: `track_model(model, ds)` with the settings-line model applied — the same value `prepare()` puts in `Prepared.model`. Builder's call whether to compute it ahead (a small helper shared with `prepare`) or move `prepare()` before the doubling block; the shadow must not start for a refused request.
4. `window = compact.window_for(ds, sent_model, app.listed_context)`; `limit = compact.over(app.anchors.estimate(key, len(body)), window)` when `window`.
5. Refused: respond 400 with `compact.error_body`, `status = 400`, record the row (reason `"compact: %dK of %dK on %s"`, no usage), log line as usual, `app.notices[sid] = {"turn": f.turn_key[:16], "text": "Asked Claude Code to compact: about %dK of %s's %dK window."}`; return before doubling and the upstream call.
6. Served downstream answer (status 200): `app.anchors.note(key, len(body), in + cache_read + cache_write)` from leg 1's usage (`leg_caps[0]` when the Splicer ran, else `cap`). A downstream `count_tokens` 200 answer: read its JSON `input_tokens` (body is small; it's already passed to the client — capture while forwarding) and `note(key, len(body), input_tokens)`.
7. Whole check in `try/except Exception`: on any error, carry on as today.

- [ ] **Step 1: Write the failing tests** (fake downstream lists `{"id": "m", "context_length": 1000}`; tier models all `m`; a session id header on every request):
  - `test_refuses_near_window`: a `/v1/messages` body of 4000 bytes (estimate 1000 ≥ 900) → status 400, body equals `error_body(1000, 900)`, the fake saw **no** `/v1/messages` call, the departures row has status 400 and reason starting `compact:`, and `GET /ui/api/lever?session=…` carries the notice.
  - `test_compaction_request_not_refused`: same body with `x-claude-code-request-class: compaction` → forwarded (fake saw it), status 200.
  - `test_no_window_no_check`: model without a window, 40 KB body → forwarded.
  - `test_anthropic_never_checked`: lever `anthropic`, same body → never 400 from signaller (point `SIGNALLER_ANTHROPIC_URL` at the fake as the existing tests do).
  - `test_anchor_from_usage`: fake answers with `usage.input_tokens: 100`; a second request 400 bytes longer → estimate 200, forwarded.
  - `test_compacted_drops_anchor`: note an anchor of 950 via a first answer; next request with `x-claude-code-context-compacted: reactive` and a 400-byte body → forwarded.
  - `test_count_tokens_anchor`: fake `count_tokens` answers `{"input_tokens": 100}` for a 4000-byte body; a `/v1/messages` of 4400 bytes → estimate 200, forwarded.
  - `test_refused_has_no_shadow`: doubling rate 100 with a shadow to anthropic → no shadow request reaches the fake Anthropic for the refused request.
- [ ] **Step 2: Run** `python3 -m pytest -q tests/test_console.py -k "refus or compaction or window or anchor or anthropic_never"` — expect FAIL.
- [ ] **Step 3: Implement** per the order above. `ponytail:` comments: the fixed 90%; bytes/4; Advisor legs 2+ unchecked.
- [ ] **Step 4: Run** `python3 -m pytest -q tests` — expect all PASS.
- [ ] **Step 5: Manual check on a scratch router** (port 8788, scratch config dir, never 8787): fake downstream with a tiny window; `ANTHROPIC_BASE_URL=http://127.0.0.1:8788 claude -p --model haiku …` twice in one session with a rule sending haiku to the fake → the second turn shows a 400 `compact:` departure, then a compaction-class request and the retried turn reach the fake.
- [ ] **Step 6: Commit** `git commit -am "router: ask Claude Code to compact before a downstream model's context window"`

### Task 5: Console — the window per model on the row

**Files:**
- Modify: `ui/app.js` (`loadModels`, `trow`'s tier block, `saveDs` use), `ui/app.css`
- Modify: `console.py` `check_downstream_edits` docstring only (context is editable like models)

**Interfaces:**
- Consumes: `GET /ui/api/models` `context`; config `downstreams[].context`.
- Produces: `CTX[track] = {model: tokens}` (listed, from `loadModels`).

- [ ] **Step 1: Implement**: `loadModels` stores `CTX[t] = j.context || {}`. Under each tier's model picker, a line `context [input] <source> · compacts at <90%>K`:
  - input value: `d.context?.[model]` (manual) else `CTX[t]?.[model]` (listed) else empty, placeholder `unknown — no check`; source text `set here` / `listed` / nothing.
  - on change: number in 1000–10000000 → `saveDs(t, { context: { ...(cur().context || {}), [model]: n } })`; empty → drop that model's key (`null` when none left); anything else → the row's existing price-style error, not saved.
  - `data-k: ctx:<t>:<tier>`; same model on two tiers shows the same value.
  - CSS: a small muted line in the tier cell, wrapping on mobile under the existing `@media` rules.
- [ ] **Step 2: Verify** `node --check ui/app.js` → no output.
- [ ] **Step 3: Browser check** against the scratch router with the fake downstream (window listed 850000): row shows `850000 listed · compacts at 765K`; type 500000 → saved (`GET state` shows `context`), source `set here`; clear it → back to `listed`; set two tiers to the same model, edit one → the other shows the same value; no console errors besides expected 502s for unkeyed downstreams.
- [ ] **Step 4: Run** `python3 -m pytest -q tests` — all PASS.
- [ ] **Step 5: Commit** `git commit -am "console: show and set each model's context window on the downstream row"`

### Task 6: Docs

**Files:**
- Modify: `README.md` (a short "Compacting for a downstream's window" paragraph under the downstream section: where windows come from, the 90% trigger, the console field), `AGENTS.md` (a routing-invariants bullet: the check, its exemptions, the undocumented headers it relies on).

- [ ] **Step 1: Write both**, matching the files' existing voice.
- [ ] **Step 2: Commit** `git commit -am "docs: compacting for a downstream's context window"`
