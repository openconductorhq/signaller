# Compact in time for a downstream's context window

## Why

Claude Code decides when to compact from the context window it assumes for the
Claude model it asked for: about 967K for a 1M model. A downstream may serve a
model with a smaller window, such as DeepSeek at 850K. Between 850K and
Claude Code's own threshold every request fails on the downstream, and Claude
Code doesn't know why. The person wants signaller, which knows which downstream
and model served each request, to make Claude Code compact in time, **without
changing Claude Code's settings**: a settings window is per Claude model, so it
would also shrink the same session's window when it runs on Anthropic.

## Goals

- A session served by a downstream compacts before that model's window is
  reached, and the compaction itself runs on the same downstream (cheap).
- The window comes from the downstream on its own where it can; a manual window
  per model covers the rest.
- The person can see, on the downstream's row, which window signaller acts on.

## Non-goals

- Anthropic-bound requests: never checked, never refused.
- Doubling shadows: not checked (a shadow never answers Claude Code).
- Long-context pricing (Valar's `long_context_pricing` threshold): a later use
  of the same mechanism, not this one.
- A configurable trigger percentage: fixed at 90% (see Decisions).

## Anti-goals (a failure even if it works)

- Refusing a request that would have fit: a session that compacts every few
  turns is worse than an occasional downstream error.
- Refusing Claude Code's compaction request: that loops. It is always sent.
- Failing closed: a missing window, an unreadable model list or an estimate that
  can't be made means the request is sent as today.

## How Claude Code behaves (checked on a real session, 2026-11-16)

A throwaway proxy answered one `/v1/messages` request with
`400 {"type":"error","error":{"type":"invalid_request_error","message":"prompt is too long: 900000 tokens > 850000 maximum"}}`.
Claude Code then, by itself:

1. sent a compaction request with `x-claude-code-request-class: compaction`
   and `x-claude-code-compaction: reactive`, on the **same model** (so rules
   send it to the same downstream), smaller than the refused turn (52 KB vs
   139 KB);
2. retried the turn on the compacted history, with
   `x-claude-code-context-compacted: reactive` (4 messages down to 2), and the
   turn completed with the earlier context preserved in the summary.

Ordinary requests carry `x-claude-code-request-class: main`. Body text is no
marker: words like "summary" and "compact" occur in every request. These
headers are undocumented internals and a Claude Code update can change them
(the same caveat as `cc_is_subagent`).

## Approaches considered

- **Refuse early in the router, compact on the downstream (chosen).** At 90%
  of the window signaller answers "prompt is too long" itself; the compaction
  request still fits on the downstream.
- **Translate the downstream's own overflow error.** Fires only at the hard
  limit, where the compaction request no longer fits on the downstream; it
  would have to run on Anthropic (~$4 per compaction on Opus at 850K).
  Rejected: the person chose cheap compaction over the last ~10% of context.
- **Plugin hook.** Hooks can block or add text to a compaction but cannot
  start one. Rejected.

## Design

### 1. Where the window comes from (per model id)

Looked up for the model id **actually sent** to the downstream (after the tier
mapping and any settings line, i.e. `Prepared.model`):

1. **Manual window** on the downstream (new config field, below). Wins.
2. **Listed window** from the downstream:
   - `GET /v1/models`: per model, the first of `context_length` (Valar; prompt
     plus output), `max_input_tokens` (LiteLLM when declared), `max_model_len`
     (vLLM). Integers > 0 only.
   - If that list gives no window for any model: LiteLLM's `GET /model/info`
     with the same key: `data[].model_name` → `model_info.max_input_tokens`.
     Several deployments under one `model_name`: take the smallest.
3. **None:** no check for that model.

Listed windows are fetched wherever signaller already fetches a downstream's
model list (the wizard's probe when a downstream is added, the console's
`GET /ui/api/models`, the row's Refresh models), and
at startup and hourly for **every** keyed downstream (today `refresh_prices`
only fetches Valar-kind ones; it grows a window fetch for all kinds). They are
saved in `models.json` under `listed_context: {downstream id: {model id:
tokens}}`, next to `listed_prices`, so a downstream that's down keeps its
windows. A payload that yields no windows must not wipe saved ones (the same
rule `listed_prices` follows). `/model/info` is only tried when `/v1/models`
gave nothing, and its failure (404 on non-LiteLLM servers) is silent.

### 2. Config: manual windows (console.json v6)

`Downstream` gains `context: dict | None` — `{model id: tokens}`, `None` or
`{}` = no manual windows. Valid values: integers 1,000–10,000,000. Config
version 5 → 6; a v5 config migrates with `context: None`. Add a
`tests/fixtures/console_v5_*.json` fixture, per `AGENTS.md`. Presets carry it
(it's portable, not machine state).

### 3. The check, in the request path

Applies only when all hold: the request is `POST /v1/messages` (not
count_tokens), it is going to a downstream (served track, not shadow), the
final model has a window, and the request isn't `x-claude-code-request-class:
compaction`. The labelling request never gets here (it goes to Anthropic).

**Estimating the request's size**, per conversation key
`(session id, x-claude-code-agent-id or "main")` — a subagent has its own
context:

- After each served downstream response, remember for that key the request
  body length and the response's input total (`input_tokens +
  cache_read_input_tokens + cache_creation_input_tokens`; with the Advisor's
  Splicer, leg 1's figures, not the summed legs).
- Estimate = remembered input total + max(0, body length now − remembered
  body length) / 4. No remembered entry (first request, restart, no session
  id): body length / 4.
- A downstream `count_tokens` answer for the same key is an anchor too: its
  `input_tokens` is exact for that body (no cache split needed). Same pair:
  body length, input total.
- A request with `x-claude-code-context-compacted` drops the key's entry
  first (the history shrank; the old anchor overstates it).
- In memory only, bounded (`BoundedDict`), lost on restart (the bytes/4
  fallback covers it).

**Refusing:** estimate ≥ 90% of the window → don't contact the downstream.
Answer `400` with `{"type": "error", "error": {"type": "invalid_request_error",
"message": "prompt is too long: <estimate> tokens > <90% figure> maximum"}}`
(the shape and wording Claude Code compacted on in the check above). Then:

- record the row as usual with status 400 and reason
  `compact: <estimate>K of <window>K on <downstream>` (so the console's
  departures show it), no tokens, no cost;
- log line as usual (`route=… reason=compact status=400 …`);
- post a session notice (`App.notices`) so the plugin's transcript line says
  signaller asked Claude Code to compact, with the figures.

A refused request gets **no doubling shadow**: the check runs before the
shadow would start, so no money goes on a turn that was never served. (The
check needs the sent model; the builder computes it ahead of the shadow, or
moves `prepare()` up.)

**Images:** the check runs whether or not the request carries an image. On a
downstream that can't take images such a session is really served by
Anthropic through the image fallback, so it compacts earlier than it needed
to; harmless, and the alternative (skipping image requests) would let an
image-capable downstream overflow.

**The Advisor's later legs** (leg 2+, opened mid-stream) are not checked: the
10% headroom is deemed to cover a leg's extra partial output, and a mid-stream
refusal can't use the 400 answer anyway.

Any exception in the check → send the request as today (never fail closed).

### 4. Console (the downstream's row)

Under each tier's model dropdown, one line for that model's window:

```
Sonnet
[ deepseek-v4.1-flash ▾ ]
context [ 850000 ] listed · compacts at 765K
```

- The input shows the effective window: the manual value if set, else the
  listed one (marked "listed"), else empty with placeholder "unknown — no
  check". Editing it sets the manual window for **that model id** (not the
  tier); clearing it drops the manual value and falls back to the listed one.
- Two tiers on the same model share one value (it's per model).
- "compacts at" is 90% of the effective window.
- `GET /ui/api/models` returns `context: {model id: tokens}` (listed) next to
  `models`, so the row can show listed values; manual ones come from the config.
  When the downstream is unreachable and the saved list is served (`stale`),
  the saved `listed_context` goes with it.

## Decisions (the builder's or mine, open to change)

- **90%, fixed** (`ponytail:` comment naming the limit). It leaves room for
  the compaction request and the answer; Valar's `context_length` counts the
  output too.
- **bytes/4** for growth and for the no-anchor fallback: conservative for text,
  generous for base64 images (an image-heavy session may compact a little
  early). Acceptable for a first version.
- Window looked up by the **sent** model id, so a settings line that swaps the
  model is checked against the right window.
- No shadow for a refused request; image requests are checked like any other;
  the Advisor's later legs aren't checked; `count_tokens` answers anchor the
  estimate (all in section 3).

## Left to the builder

- Exact names: the `App` map, the `models.json` key (proposed
  `listed_context`), the reason text.
- How the row's context input is laid out within the existing grid and mobile
  rules.
- Test split: `unwire`-style pure tests for the window lookup and the estimate;
  one console API test (real HTTP, a fake downstream answering `/v1/models`
  and `/model/info`) for the refuse path, including that a `compaction`-class
  request over the limit is still sent; a migration test with the v5 fixture.
