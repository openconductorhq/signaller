# The Advisor on a downstream

## Why

Since Claude Code 2.1.3xx every main-loop request carries the Advisor:

```json
{"type": "advisor_20260301", "name": "advisor", "model": "claude-opus-5-5", "defer_loading": true}
```

On Anthropic, the model may call it mid-answer; Anthropic's API runs the named
model over the transcript and puts its advice into the same response. Claude
Code never runs it. A downstream can't run it either, so today (`69f64e5`)
`strip_advisor` drops it on the way to a downstream and a downstream turn
works without advice.

This design lets a downstream turn have the Advisor too, served by the
downstream itself: the model the downstream maps the Advisor's tier to.
Anthropic is not involved. Signaller plays the part Anthropic's API plays: it
runs the call.

## What Claude Code expects back

Read from 2.1.322's stream handling. In the assistant message, in order:

```json
{"type": "server_tool_use", "id": "srvtoolu_…", "name": "advisor", "input": {}}
{"type": "advisor_tool_result", "tool_use_id": "srvtoolu_…",
 "content": {"type": "advisor_result", "text": "<advice>", "stop_reason": "end_turn"}}
```

On failure the content is `{"type": "advisor_tool_result_error", "error_code":
"unavailable"}` (other codes: `overloaded`, `prompt_too_long`,
`execution_time_exceeded`, …). Both blocks then sit in the history of later
requests, inside the assistant message, followed by the rest of the answer.

## Shape

### 1. Out: the request to the downstream (`prepare`)

`strip_advisor` becomes `advisor_out(req, ds)`:

- **The definition:** an `advisor_…` tool whose `model` maps to a downstream
  model (always, while every downstream has a model per tier) is replaced by
  an ordinary tool:
  `{"name": "advisor", "description": "<when to ask a stronger model>",
  "input_schema": {"type": "object", "properties": {}}}`.
  The model the call will use is worked out here, with the same
  `track_model` / settings-line resolution as the served request, for
  `tier_of(advisor model)`. The window is the served request's own: whatever
  this downstream is sending right now (its tier row, a settings line, the Now
  fallback). A window the advisor model isn't priced for falls back to Now,
  the rule `prepare()` already applies. Any downstream works the same way;
  nothing here is Valar's (a downstream without windows sends none).
- **The history:** each `server_tool_use` named advisor plus its
  `advisor_tool_result` becomes a client tool round trip. The assistant
  message is split at the call: `[…, tool_use(advisor)]`, then
  `user [tool_result(text, is_error on an error result)]`, then
  `assistant [the rest]`. Its id is kept, so the pair matches.
- Nothing about an Anthropic-bound request changes: byte for byte, as now.

### 2. Back: running the call (the proxy loop)

Only when step 1 offered the tool. Otherwise the stream passes through as now.

1. **Leg 1:** stream the downstream's answer to Claude Code, parsing SSE
   events (they are already read chunk by chunk for `AnswerCapture`).
2. **The call:** a `content_block_start` of a `tool_use` named `advisor` is
   held back, with everything after it in that message (its input deltas,
   `message_delta`, `message_stop`).
3. **The advice:** a non-streaming request to the same downstream with the
   advisor's model: leg 1's system and messages, plus the assistant message so
   far, plus one user line asking for advice on what to do next. Tools stay in
   the request (a history with `tool_use` needs them) with
   `tool_choice: {"type": "none"}`. `max_tokens` 4096. ponytail: no streaming
   of the advice; Claude Code shows it when it lands.
4. **To Claude Code:** the `server_tool_use` and `advisor_tool_result` blocks
   above, at the next content index.
5. **Leg 2:** the original request plus the assistant message so far ending in
   `tool_use(advisor)`, plus `user [tool_result(advice)]`. Its stream is
   forwarded with `message_start` dropped and content indices shifted after
   the blocks already sent, to the end. The next `advisor` call in leg 2 loops
   back to step 2.
6. **Usage:** the final `message_delta` carries the summed output tokens;
   input and cache tokens stay leg 1's (what Claude Code shows as context).

Limits:

- **Calls per request:** at most 3. After the third, leg N goes out without
  the tool.
- **An advisor call next to other tool calls** in one message: the advice is
  run and its blocks emitted, the other calls pass through and the message
  ends with `stop_reason: tool_use` (no leg 2). Claude Code runs them; the
  advice is in the history for the next request.
- **Non-streaming requests:** the same loop over JSON bodies. Claude Code's
  main loop streams, so this is mostly for side requests and tests.

### 3. Failure

Never fail closed:

- **Leg 1 fails** before any call: as today (status passed through, image
  fallback unchanged).
- **The advice fails** (error, timeout, unpriced or keyless): Claude Code gets
  `advisor_tool_result_error` with `unavailable`, and leg 2 runs with an
  `is_error` tool result. The turn continues without advice.
- **Leg 2 fails mid-stream:** `MIDSTREAM_SSE`, as any mid-stream error today.
- **A parse failure** of leg 1's stream: stop intercepting, pass the rest
  through untouched.

### 4. Records and console

- **Each advice call is a request of its own:** recorded in `requests` like
  any other, with its own id, timestamps, status, tokens and cost: `track` the
  downstream, `reason` `advisor`, `model_in` the advisor model Claude Code
  named (`claude-opus-5-5`), `model_out` the downstream model it ran on,
  `session_id` and `turn_id` the parent's. One new column, `parent_id`, the
  request that called it (fresh `SCHEMA`, `COLS`, the `_migrate` map). The
  parent's `cost` and `ms` stay its own legs only, so nothing is counted
  twice in the summary, sessions or money cards.
- **Departures:** advice calls show as rows in the list like every request,
  newest first. The row reads `advisor` in the why column, with the model and
  latency like any other, and a `↳ for #<parent>` mark. The parent row gets
  an `advisor ×N` mark. Opening an advice row shows its model, status,
  timings and tokens, and the parent's prompt as "The prompt it was advising
  on" (the advice text itself isn't stored, like any answer). The filters
  work on it (downstream, latency; the Anthropic-model filter never matches
  it, since it isn't billed by Anthropic).
- **Errors:** a failed advice call is an error row like any other, so it
  counts in "errors in the last hour" and shows in that modal.
- **Log line:** each advice call logs its own line, `reason=advisor`. Never
  the advice text.

### 5. What it doesn't touch

- **Doubling:** a downstream shadow keeps `strip_advisor` (no advice run for
  a shadow). An Anthropic shadow is untouched.
- **Routing:** unchanged. The Advisor isn't a `servertool` (`69f64e5`).
- **Thinking:** a downstream's thinking blocks in the partial assistant message
  go to the advice request and leg 2 as they came; `drop_old_thinking` still
  guards the way back to Anthropic.

## Off switch

Per downstream, `advisor: true|false` in its config entry, default true. A
config shape change: version 5, migration in `routing.config_from_json`,
fixture under `tests/fixtures/`. Off means today's `strip_advisor`. Claude
Code's own `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` turns the Advisor off
everywhere.

## Tests

- `advisor_out`: the definition swapped, a history pair split and matched,
  an error result as `is_error`, an Anthropic request untouched.
- Proxy, against fake upstreams: one call (indices, blocks, usage), two calls,
  the cap, a call next to a client tool, the advice failing, leg 2 failing.
- Store: `parent_id` migrates onto an old database; an advice row is
  recorded with its parent's session and turn.
- Console: an advice call is its own departure, linked to its parent; the
  summary doesn't count its cost twice.

## Open questions

1. **The advice prompt:** Anthropic's advisor prompt isn't public. The
   advice request gets the whole transcript (system, messages, the
   assistant's message so far) and then one user line:

   > You are an advisor to the assistant in this conversation, a stronger
   > model it consulted mid-task. You can't run tools. Read the conversation
   > and the assistant's work so far, then advise it on what to do next:
   > what it may have missed or got wrong, the risks in its current
   > approach, and a concrete next step. Be brief and specific. Don't redo
   > its work, and don't address the user.

   Tune it on real turns.
2. **Calls per request:** at most 3 (the next request resets the count), so
   a model that keeps calling the advisor can't loop. Anthropic caps its own
   server tools the same way (`max_uses`).

## As built

Where the shipped code differs from the text above:

- **Pricing:** advice rows are priced on Anthropic too (`api_cost` set, from
  the model Claude Code named).
- **Functions:** there is no `advisor_out`. The pieces are `to_client_form`,
  `strip_advisor` and `drop_issued`.
- **Which requests:** only a streaming `/v1/messages` to a downstream with the
  switch on gets the Advisor. Everything else (non-streaming, shadows, switch
  off) strips it; it is not "the same loop over JSON bodies".
- **The switch:** each downstream has an `advisor` switch (config v5, default
  on).
- **Pings:** the router writes an SSE ping every `PING_EVERY` while the advice
  runs, and keeps pinging through leg 2's connect and first read.
- **Issued ids:** advisor blocks the router issues carry ids starting
  `srvtoolu_sgnl_`. `drop_issued` removes them on the way to Anthropic;
  Anthropic's own pairs stay. Leg 2 uses the same issued id, so the pair is what
  the next request carries.
- **`turn_id`:** advice rows carry none (this amends section 4). `parent_id`
  links them; the console treats any row with a `turn_id` as doubled.
- **Several advisor calls in one message:** only the first is spliced; later
  ones are dropped.
- **Billing:** the served row bills every leg's usage. The stream shows Claude
  Code leg 1's input and cache tokens (read from leg 1's `message_start` and
  `message_delta`), with output summed.
- **Path:** the advice call posts to the served request's path and query.
- **`prepare()`:** returns a `Prepared` NamedTuple (the `AdvisorPlan` last)
  and takes `raw=`, the original body.
