# The Advisor on a Downstream: Implementation Plan

> Superseded in places by the "As built" section of the design spec.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A turn served by any downstream gets Claude Code's Advisor, run by signaller on that downstream's model for the Advisor's tier, and each advice call shows as its own row in Departures.

**Architecture:** A new pure module `advisor.py` holds every request rewrite and the SSE splicing (no I/O). `prepare()` swaps the Advisor definition and history for a client-tool form and returns an `AdvisorPlan`. The proxy in `signaller.py` runs legs: it streams leg 1 through a `Splicer`, and on an `advisor` call it makes the advice request, emits Anthropic-shaped blocks, then streams leg 2. Each advice call is recorded as a `requests` row with `parent_id`.

**Tech Stack:** Python 3 standard library only (router); vanilla JS/CSS (console); pytest; Claude Code's plugin test (untouched).

**Spec:** `docs/superpowers/specs/2026-11-11-downstream-advisor-design.md`

## Global Constraints

- Router: standard library only, runs as `python3 -E -s`; no new dependencies.
- Never fail closed: any advisor fault leaves the turn running without advice.
- An Anthropic-bound request goes out byte for byte, as now.
- Never log, store or return the advice text or any prompt text; log lines stay `route= reason= model= status= ms= stream= [err=] shadow=`.
- All store writes go through `Store._q` (`record_request`).
- The Advisor definition Claude Code sends: `{"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5", "defer_loading": true}`.
- Blocks back to Claude Code: `{"type": "server_tool_use", "id": "srvtoolu_…", "name": "advisor", "input": {}}`, then `{"type": "advisor_tool_result", "tool_use_id": …, "content": {"type": "advisor_result", "text": …, "stop_reason": "end_turn"}}`; on failure the content is `{"type": "advisor_tool_result_error", "error_code": "unavailable"}`.
- At most 3 advisor calls per request (`MAX_CALLS = 3`).
- Advice request: `max_tokens` 4096, `stream` false, tools kept, `tool_choice: {"type": "none"}`.
- Advice window: the served request's window; a window the advisor model isn't listed for falls back to `asap` (the rule in `prepare()`).
- Config goes to version 5: each downstream gets `advisor: bool`, default true. Migration in `routing.config_from_json`, fixture `tests/fixtures/console_v4_advisor.json`.
- Test on a scratch router on port 8788 with a scratch config dir; never the live 8787.
- The advice prompt, verbatim:
  > You are an advisor to the assistant in this conversation, a stronger model it consulted mid-task. You can't run tools. Read the conversation and the assistant's work so far, then advise it on what to do next: what it may have missed or got wrong, the risks in its current approach, and a concrete next step. Be brief and specific. Don't redo its work, and don't address the user.

## Review Focus

- **The downstream ignores `tool_choice: none`** and answers the advice request with a tool call and no text: treat it as an empty advice, sent as an `advisor_result` with text `"(no advice)"`, never as a crash. Test in Task 2 (`advice_text`) and Task 4.
- **Leg 1 ends with `max_tokens` while the advisor call's input is still streaming:** a cut-off call isn't run; the held events are flushed as they came. Test in Task 3.
- **Claude Code cancels mid-advice** (the client socket closes while the advice request runs): no leg 2 is sent, and the advice row is still recorded. Test in Task 4.
- **A history with an `advisor_tool_result` and no matching `server_tool_use`** (a compacted or edited conversation): the orphan becomes nothing, not an unmatched `tool_result`. Test in Task 2.
- **A non-streaming request carrying the Advisor** (a side request): offered the tool and handled over JSON, or the tool stripped. Plan: strip it (ponytail), test in Task 4.

---

### Task 1: Config v5, the per-downstream switch

**Files:**
- Modify: `routing.py` (`Downstream`, `_downstream`, `config_from_json`, `config_to_json`)
- Create: `tests/fixtures/console_v4_advisor.json` (a copy of `console_v4_auto.json`)
- Test: `tests/test_routing.py`

**Interfaces:**
- Produces: `Downstream.advisor: bool = True` (last field); config `"version": 5`.

- [ ] **Step 1: Write the failing tests** in `tests/test_routing.py`:

```python
def test_v4_config_migrates_to_advisor_on(self):
    cfg = config_from_json(json.load(open("tests/fixtures/console_v4_advisor.json")))
    self.assertTrue(all(d.advisor for d in cfg.downstreams))
    self.assertEqual(config_to_json(cfg)["version"], 5)

def test_advisor_switch_roundtrips_and_validates(self):
    j = config_to_json(DEFAULT_CONFIG)
    j["downstreams"][0]["advisor"] = False
    self.assertFalse(config_from_json(j).downstreams[0].advisor)
    j["downstreams"][0]["advisor"] = "no"
    with self.assertRaisesRegex(ValueError, "advisor must be true or false"):
        config_from_json(j)
```

- [ ] **Step 2: Run them to see them fail.** Run `uvx --quiet pytest -q tests/test_routing.py -k advisor`. Expected: FAIL (`advisor` attribute missing).
- [ ] **Step 3: Implement it.** Version 4 migrates to 5 by giving every downstream `advisor: True`. `_downstream` reads `d.get("advisor", True)`, raising `downstream {i}: advisor must be true or false` for a non-bool. `config_to_json` writes version 5 (`asdict` carries the field). Any other test or fixture expecting `"version": 4` moves to 5.
- [ ] **Step 4: Run the whole suite.** Run `uvx --quiet pytest -q tests`. Expected: all pass.
- [ ] **Step 5: Commit** `routing.py tests/` with "Config v5: an advisor switch per downstream".

### Task 2: `advisor.py`: the request rewrites

**Files:**
- Create: `advisor.py`
- Modify: `signaller.py` (`strip_advisor` moves into `advisor.py`, imported back so `tests/test_console.py::StripAdvisorTest` keeps passing)
- Test: `tests/test_advisor.py`

**Interfaces:**
- Produces (all pure, editing `req` in place where noted):
  - `MAX_CALLS = 3`, `PROMPT` (the spec's text), `TOOL = {"name": "advisor", "description": …, "input_schema": {"type": "object", "properties": {}}}`
  - `definition(req: dict) -> dict | None`: the `advisor_…` tool, or None.
  - `strip_advisor(req: dict) -> bool`: as today.
  - `to_client_form(req: dict) -> bool`: swap the definition for `TOOL`, and turn each `server_tool_use(advisor)` + `advisor_tool_result` pair into a split round trip: assistant `[…, tool_use(id, "advisor", {})]`, user `[tool_result(id, text, is_error)]`, assistant `[rest]`. An orphan result or call is dropped. Empty messages are dropped. True if anything changed.
  - `advice_request(req: dict, partial: list, model: str) -> dict`: a new body (deep copy). Same system, tools and messages; the assistant `partial` blocks appended (without the advisor `tool_use`), then user `[{"type": "text", "text": PROMPT}]`. `model`, `max_tokens` 4096, `stream` false, `tool_choice {"type": "none"}`. No `thinking`.
  - `continue_request(req: dict, partial: list, call_id: str, advice: str, error: bool, offer: bool) -> dict`: a deep copy with assistant `partial + [tool_use(call_id)]` and user `[tool_result(call_id, advice, is_error=error)]` appended. With `offer` false, `TOOL` is removed.
  - `advice_text(body: dict) -> str`: the text blocks joined, or `"(no advice)"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_advisor.py`:
  - `test_definition_found_and_missing`: `definition` returns the dict for an `advisor_20260301` tool and None for `[{"name": "Bash"}]`.
  - `test_to_client_form_swaps_definition`: tools become `[{"name": "Bash"}, TOOL]`.
  - `test_to_client_form_splits_a_pair`: assistant `[text "a", server_tool_use(id "s1", advisor), advisor_tool_result(s1, {"type": "advisor_result", "text": "do X"}), text "b"]` becomes three messages: assistant `[text a, tool_use s1 advisor {}]`, user `[tool_result s1 "do X"]`, assistant `[text b]`.
  - `test_to_client_form_error_result`: an `advisor_tool_result_error` content gives `tool_result(is_error=True, content="advisor unavailable")`.
  - `test_to_client_form_drops_orphans`: a result with no call, and a call with no result, both vanish; True returned.
  - `test_to_client_form_untouched_without_advisor`: returns False and `req` is unchanged.
  - `test_advice_request_shape`: the model, `max_tokens == 4096`, `stream is False`, `tool_choice == {"type": "none"}`, the last user text `== PROMPT`, the original `req` unchanged.
  - `test_continue_request_shape`: the last two messages are the call and its result; with `offer=False` there's no tool named `advisor`.
  - `test_advice_text_empty`: `advice_text({"content": [{"type": "tool_use"}]}) == "(no advice)"`.
- [ ] **Step 2: Run them to see them fail.** Run `uvx --quiet pytest -q tests/test_advisor.py`. Expected: FAIL (no module `advisor`).
- [ ] **Step 3: Implement** `advisor.py` with the interfaces above. `TOOL`'s description: "Ask a stronger model for advice on the task: what you may be missing and what to do next. Takes no input; it reads the whole conversation."
- [ ] **Step 4: Run** `uvx --quiet pytest -q tests`. Expected: all pass.
- [ ] **Step 5: Commit** `advisor.py signaller.py tests/test_advisor.py` with "Advisor: request rewrites for a downstream".

### Task 3: `advisor.Splicer`: the SSE event rewriting

**Files:**
- Modify: `advisor.py`
- Test: `tests/test_advisor.py`

**Interfaces:**
- Produces: `class Splicer`:
  - `Splicer()`; `.feed(chunk: bytes) -> list[bytes]`: the SSE bytes to send to the client now. Events before an advisor call pass through as they came, except that `message_start` is dropped on any leg after the first and content indices are shifted by `.offset`.
  - After a `content_block_start` of `tool_use` named `advisor`, it holds that event and everything after it, and sets `.call = {"id": str}` once `message_delta` arrives with `stop_reason == "tool_use"` and that block is the only `tool_use` in the message.
  - `.partial: list[dict]`: the assistant content blocks of this leg up to (not including) the call, rebuilt from the deltas (text, thinking with its signature, tool_use with parsed input).
  - `.mixed: bool`: true when the same message also holds another `tool_use`. `.call` is still set, and everything from the advisor block on is held, the other blocks' events included (a block that streamed before the advisor call is already out). After `advice_events(...)`, `.rest() -> list[bytes]` returns the held events of the other blocks and the message tail (`message_delta`, `message_stop`), their content indices shifted past the two advice blocks, with the advisor block's own events dropped. The proxy writes `.rest()` and ends the turn with no leg 2 (`stop_reason` stays `tool_use`; the spec's "advisor call next to other tool calls").
  - `.flush() -> list[bytes]`: the held events as they came, for a call that isn't run (a `max_tokens` stop, a parse failure).
  - `.advice_events(call_id: str, text: str | None, error: bool) -> list[bytes]`: the `content_block_start`/`content_block_stop` pairs for `server_tool_use` and `advisor_tool_result` at the next indices, advancing the offset for the next leg.
  - `.next_leg()`: resets per-leg state, keeping `.offset` and `.out_tokens`.
  - `.out_tokens: int`: output tokens summed over legs; the last leg's `message_delta` is rewritten to carry it.
  - Any exception inside `feed` sets `.broken`; from then on `feed` returns its input unchanged.
  - The id sent to Claude Code is `"srvtoolu_" + call["id"]`. The call and its result carry the same id, so `to_client_form` turns them back into a matching pair on the next request without any mapping.

- [ ] **Step 1: Write the failing tests**, building SSE with a helper `sse(*events) -> bytes`:
  - `test_passes_plain_stream_through`: no advisor call means the output equals the input, chunked at every byte.
  - `test_holds_advisor_call`: text block 0, `tool_use(advisor)` at index 1, `message_delta(stop_reason tool_use)`, `message_stop`. Only block 0's events come out; `.call["id"]` is set; `.partial == [{"type": "text", "text": …}]`.
  - `test_advice_events_and_leg2_indices`: after `advice_events("x", "do X", False)`, `next_leg()`, and a leg 2 of `message_start` + text block at index 0 + `message_delta(output_tokens 5)`: leg 2 has no `message_start`, its text is at index 3 (0 text, 1 server_tool_use, 2 result), and the final delta's `output_tokens` is leg 1's plus 5.
  - `test_mixed_tool_calls_keep_the_other_call`: text block 0, advisor `tool_use` at index 1, Bash `tool_use` at index 2, `message_delta(stop_reason tool_use)`, `message_stop`. `.mixed` is True and `.call["id"]` is set. After `advice_events("x", "do X", False)`, `rest()` holds Bash's events at index 3 (0 text, 1 server_tool_use, 2 result, 3 Bash), none of the advisor block's, then the delta and stop.
  - `test_max_tokens_cut_call_is_flushed`: `stop_reason max_tokens` means `.call` is None and `flush()` returns the held events byte for byte.
  - `test_unparseable_event_breaks_to_passthrough`: a `data: {bad` line sets `.broken`, and the rest passes unchanged.
- [ ] **Step 2: Run them to see them fail.** Run `uvx --quiet pytest -q tests/test_advisor.py -k "passes or holds or advice_events or mixed or max_tokens or unparseable"`. Expected: FAIL.
- [ ] **Step 3: Implement `Splicer`.** Parse on `\n\n` event boundaries, keeping each event's raw bytes so passthrough is exact. Rewrite only events whose index or `output_tokens` changes (re-serialised `event: <type>\ndata: <json>\n\n`).
- [ ] **Step 4: Run** `uvx --quiet pytest -q tests`. Expected: all pass.
- [ ] **Step 5: Commit** with "Advisor: splice an advice call into a downstream's stream".

### Task 4: The proxy runs the call

**Files:**
- Modify: `signaller.py` (`prepare`, its two call sites in `handle_any`, `handle_any`'s stream loop), `doubling.py` (line 207 unpacks `prepare`'s result: nine names become ten)
- Modify: `store.py` (`SCHEMA`, `COLS`, `_migrate` types: `parent_id INTEGER`)
- Test: `tests/test_proxy.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: Task 1 `Downstream.advisor`; Task 2 `to_client_form`, `strip_advisor`, `advice_request`, `continue_request`, `advice_text`, `MAX_CALLS`; Task 3 `Splicer`.
- Produces:
  - `AdvisorPlan = namedtuple("AdvisorPlan", "model window")` in `signaller.py`.
  - `prepare(app, track, model, req, headers, routing=None, f=None, advise=False)` returns a **10-tuple**, adding `plan: AdvisorPlan | None` last. `advise=True` only from `handle_any` for a streaming `/v1/messages` request. With `advise`, a downstream with `ds.advisor`, and a definition present, it calls `to_client_form` and works out the plan: the model is `track_model(definition["model"], ds)`, then a settings-line `model` for `tier_of(definition model)` via `overrides.resolve`; the window is the served request's final `window`, falling back to `asap` when `app.prices.listed` lists the advisor model without it. Otherwise `strip_advisor`, as today. `doubling.py` passes nothing (shadows strip).
  - Rows: an advice call is `record_request`ed with `reason "advisor"`, `model_in` the definition's model, `model_out` `plan.model`, `window` `plan.window`, `parent_id` the served request's `rid`, its own `next_id()`, `ts`, `ms`, `ttfb_ms`, `status`, usage and `cost` (`app.prices.cost`), and `api_cost` None. It shares the parent's `session_id`, `turn_id`, `preview` and `prompt`, with `explanation` `[{"rule": "Advisor", "outcome": "asked by #<rid> → <plan.model>", "decided": true}]`. A log line goes out per call with `reason=advisor`.
  - The `AnswerCapture` is fed the spliced bytes written to the client, not each leg's raw bytes. The served row's `in_tok` and cache tokens are then leg 1's, its `out_tok` the sum over legs, and its `cost` covers its own legs only.

- [ ] **Step 1: Write the failing tests.** In `tests/test_store.py`, `test_parent_id_migrates`: open a database created with the old `requests` columns, then `record_request` a row with `parent_id=3` and read it back. In `tests/test_proxy.py`, give `FakeHandler` a scripted mode: `self.server.script`, a list of `(status, content_type, body_bytes)` answered in order and recording each body. Tests:
  - `test_advisor_call_runs_on_the_downstream`: leg 1 streams text, then `tool_use(advisor)`; the advice answer is JSON `{"content": [{"type": "text", "text": "do X"}], "usage": {...}}`; leg 2 streams text. Assert: three upstream bodies; body 2 has model = Valar's opus model, `tool_choice none` and the last user text = `advisor.PROMPT`; body 3 ends with `tool_use`/`tool_result "do X"`. The client stream holds `server_tool_use` then `advisor_tool_result` with `advisor_result` text `"do X"`, then leg 2's text. `departures()` holds two rows, the advice row with `reason "advisor"` and `parent_id` = the served row's id.
  - `test_advice_failure_continues_without_advice`: the advice answer is a 529. The client gets `advisor_tool_result_error` `unavailable`, body 3's `tool_result` has `is_error` true, and the advice row has status 529.
  - `test_cap_of_three_calls`: legs 1 to 3 each call the advisor; body 7 (leg 4) carries no tool named `advisor`.
  - `test_advisor_off_strips`: with `advisor=False` on the downstream, there's one upstream body with no `advisor` tool.
  - `test_anthropic_request_untouched`: a request routed to Anthropic reaches it byte for byte, with the Advisor definition.
  - `test_client_gone_mid_advice`: the client closes during the advice request. No body 3 is sent, and the advice row is recorded.
  - `test_non_streaming_strips`: a non-streaming downstream request carries no `advisor` tool.
  - `test_advisor_next_to_another_tool_call`: leg 1 holds text, the advisor call and a Bash `tool_use` in one message. Two upstream bodies only (no leg 2). The client stream holds `server_tool_use`, `advisor_tool_result` (`"do X"`), then the Bash `tool_use` at the shifted index, and ends with `stop_reason tool_use`.
- [ ] **Step 2: Run them to see them fail.** Run `uvx --quiet pytest -q tests/test_proxy.py tests/test_store.py -k "advisor or advice or cap_of or client_gone or non_streaming or parent"`. Expected: FAIL.
- [ ] **Step 3: Implement.** In `handle_any`, when `plan` is set, the response is SSE and status 200, the existing write loop feeds chunks through a `Splicer` and writes what it returns. When a leg ends with `.call`:
  1. If `calls == MAX_CALLS`, write `flush()` and stop (unreachable after the cap, since the tool isn't offered then).
  2. Post `advice_request` to the same `base`, path `/v1/messages`, with the same headers and the window header set to `plan.window`, through `open_upstream`, and record its row.
  3. Write `advice_events`. If `.mixed`, write `.rest()` and finish the stream: no leg 2.
  4. Otherwise open leg 2 with `continue_request(..., offer=calls < MAX_CALLS)`, call `next_leg()` and keep looping.

  Any exception in this branch writes `flush()` if nothing was sent for the call, else `MIDSTREAM_SSE`, and records `err=advisor`. Never let it escape `handle_any`.
- [ ] **Step 4: Run** `uvx --quiet pytest -q tests`. Expected: all pass.
- [ ] **Step 5: Commit** with "Advisor: run a downstream turn's advisor calls on the downstream".

### Task 5: Console: advice rows, the parent mark and the switch

**Files:**
- Modify: `ui/app.js` (`shortReason`, `depRow`, `explain`, the Tracks row), `ui/app.css`
- Modify: `README.md` (one paragraph under the downstreams section: the Advisor on a downstream, the switch, `CLAUDE_CODE_DISABLE_ADVISOR_TOOL`)
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: Task 4's rows (`reason "advisor"`, `parent_id`); Task 1's `advisor` field via `GET /ui/api/config`.

- [ ] **Step 1: Write the failing test** `test_departures_carry_parent_id` in `tests/test_console.py`: record a parent row and an advice row (`parent_id`) through `app.store`, `GET /ui/api/departures`, and assert the advice row has `parent_id` and `reason == "advisor"`. `summary`'s cost is the sum of both rows' `cost`, each counted once.
- [ ] **Step 2: Run it to see it fail.** Run `uvx --quiet pytest -q tests/test_console.py -k parent_id`. Expected: FAIL until Task 4's column exists. If it already passes, keep it as the regression guard.
- [ ] **Step 3: Implement the console changes.**
  - `shortReason("advisor")` returns `"advisor"`.
  - `depRow` adds `el("span", {class: "adv"}, "↳ for #" + r.parent_id)` when `r.parent_id`. A parent shows `advisor ×N`, N counted from the advice rows already in `#deps` whose `parent_id` matches (ponytail: a count from loaded rows, so it's short when advice rows are paged out).
  - `explain` labels the prompt "The prompt it was advising on" for an advice row.
  - The Tracks row gets an "Advisor" checkbox saving `advisor` through `saveDs`.
- [ ] **Step 4: Check it in a browser.** Start a scratch router: `SIGNALLER_CONFIG_DIR=<scratch> SIGNALLER_LISTEN=127.0.0.1:8788 python3 -E -s signaller.py`. Seed rows with a short `store.Store(...)` script, then check the advice row, the `↳ for #` mark, `advisor ×N`, the filters, the errors modal for a failed advice row, and the checkbox saving. Run `node --check ui/app.js`. Expected: clean.
- [ ] **Step 5: Commit** with "Console: advisor calls as their own departures; the advisor switch".

### Task 6: End to end on a scratch router

**Files:** none changed unless a fault turns up.

- [ ] **Step 1: Point a session at the scratch router** with the user's Valar key added through the scratch console (never copied from `downstreams.keys.json`). Use a rule or lever that sends the turn to Valar: `ANTHROPIC_BASE_URL=http://127.0.0.1:8788 _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1 claude`.
- [ ] **Step 2: Ask for something where advice is likely** ("ask your advisor before you start, then …"). Expected: Claude Code shows the Advisor call and its advice, and the turn finishes. Departures shows the served row and an `advisor` row on Valar's Opus model with `↳ for #<id>`.
- [ ] **Step 3: Run the gates.** Run `uvx --quiet pytest -q tests`, `claude plugin test plugin/signaller` and `node --check ui/app.js`. Expected: all pass.
