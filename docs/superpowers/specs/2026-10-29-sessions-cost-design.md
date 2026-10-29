# claude-router: per-session view and cost — design

Date: 2026-10-29
Status: draft, awaiting approval (builder check done)
Builds on: `2026-10-28-valar-track-design.md`, `2026-10-17-console-design.md`,
`2026-10-16-claude-router-design.md`. Everything there holds unless this
document changes it.

Mockups (approved; open in a browser from this folder, they use `ui/app.css`):

- `2026-10-29-sessions-cost/sessions-page-v3.html`: the whole console with
  2 of 4 sessions picked. Approved as-is ("this is perfect").
- `2026-10-29-sessions-cost/timeframe-v4.html`: option **A** chosen (one time
  control for the diagram and the cost). Option B was rejected.

## Intent

The console today mixes every Claude Code session that goes through the router
into one picture. The owner runs several sessions at once and wants to judge
routing **per session**: pick one or more sessions and see everything the
console shows (traffic split, rules, requests, side-by-side) for just those,
plus **what each downstream cost** and **how much routing off Anthropic
saved**.

This is the first of two projects. The second, later, lets a session or a
group of sessions have its own tracks and rules ("steering"). This one builds
the base the second needs: every request knows its session.

## Goals

1. Every recorded request carries its Claude Code **session id**; every session
   gets a readable label: `<project folder> · <git branch>` and its start time.
2. A **session picker** (chips under the header, mockup v3). Nothing picked =
   all sessions. Picking filters **everything** on the page:
   - live diagram: percentages, req/min, and the moving dots
   - cost cards
   - rulebook: a new per-rule "decided N of M" count for the shown requests
   - departures list (each row gets a small session tag)
   - side by side: only doubled turns from the picked sessions; the verdict
     tally counts only them
3. A **time control** next to the chips: `15 min · 1 hour · 24 h · Whole
   session · Custom…` (mockup v4, option A). It drives the diagram **and** the
   cost cards (and the rule counts). Custom = from/to date + time.
   "Whole session" = from the first request of the earliest picked session
   (all sessions picked: from the oldest kept request) until now.
4. **Cost cards**, one per downstream plus a savings card:
   - Anthropic: `$0 · subscription`, with "$X at API prices" underneath.
   - Valar: real cost from Valar's own published prices.
   - Local: `$0`.
   - Savings (dark card): what the Valar + local requests would have cost on
     Anthropic at API prices, minus what they actually cost.
   - Each card also shows tokens in / out.
5. An opened departure shows its tokens, its cost, and what it would have cost
   on Anthropic.

## Non-goals

- Per-session tracks or rules (the next project). The rulebook gets a grey
  "Rules for **all sessions**" line now (mockup v3), **without** the "give
  picked sessions their own rules…" link.
- Session info for requests recorded before this ships: those rows have no
  session and no tokens. They count under "All sessions" for traffic share and
  show no cost. No backfill.
- Budgets, alerts, or exporting cost.
- Exact Anthropic billing: the subscription is flat; the "API prices" figure is
  an estimate (see Cost).

## Anti-goals (would make it a failure even if it works)

- **The look changes.** The owner likes today's theme. New pieces use the
  existing vocabulary only, as in the mockups: white cards with a coloured left
  border, Barlow Condensed numbers, the segmented control from "Otherwise",
  chips in the lever style (selected = ink background).
- Anything that slows or breaks the proxied request. Session lookup, token
  counting and cost math must never fail or delay a request; on any error the
  request still goes through and the row records nulls.
- Reading a session's transcript contents. Only the `cwd` and `gitBranch`
  fields of the transcript's first lines are read.

## Facts (probe 2026-10-29)

- Claude Code (cli 2.1.312) sends `X-Claude-Code-Session-Id: <uuid>` on every
  `/v1/messages` request, and the same id inside
  `metadata.user_id` (a JSON string: `{"device_id", "account_uuid",
  "session_id"}`). Stable for the whole session.
- The project folder is **not** in the headers or `metadata`. Claude Code
  writes each session's transcript to
  `~/.claude/projects/<encoded cwd>/<session id>.jsonl`; its lines carry
  `"cwd"` and `"gitBranch"`. The folder name encoding is lossy (`/` and `-`
  both become `-`), so read `cwd` from the file, don't decode the folder name.
  `CLAUDE_CONFIG_DIR`, when set, replaces `~/.claude`.
- Subagents belong to their parent session: their transcripts live at
  `<claude dir>/projects/<cwd>/<parent id>/subagents/agent-<id>.jsonl` with
  `"sessionId": "<parent id>"`, `"isSidechain": true`. So their requests almost
  certainly send the parent's id. Not yet seen on the wire: the builder confirms
  first (a session that spawns a subagent, through a logging pass-through in
  front of the router). Subagent traffic **counts toward the parent session**;
  if the wire shows a different id, map it to the parent via that
  `subagents/` path (folder name = parent id), without reading the file.
- Valar `GET /v1/models` returns, per model, `pricing`: a list of
  `{tier, input_per_1m_tokens, cached_input_per_1m_tokens,
  output_per_1m_tokens}` for tiers `realtime, priority, standard, flex,
  batch`. `valar-auto-balanced` has empty pricing.
- Today `list_models` (console.py) keeps only the ids and `models.json` (the
  saved model list) stores only ids.
- The router keeps 30 days of requests (`RETAIN` in store.py).
- `AnswerCapture` (doubling.py) already parses SSE / JSON responses, including
  gzip, and pulls `output_tokens`; it's only created for doubled turns today.

## Design

### Recording

`requests` gains columns (add with `ALTER TABLE … ADD COLUMN` when missing; old
rows stay null):

| column | what |
|---|---|
| `session_id` | from `X-Claude-Code-Session-Id`, else from `metadata.user_id`'s `session_id`, else null |
| `in_tok`, `out_tok`, `cache_read_tok`, `cache_write_tok` | from the response `usage` (streamed: `message_start` + final `message_delta`) |
| `cost` | what this request cost in USD on the track that served it (Anthropic: 0) |
| `api_cost` | what it would have cost on Anthropic at API prices, for the Claude model the client asked for (`model_in`) |

Costs are computed **when the request is recorded** and stored, so later price
changes don't rewrite history.

Usage is read with the response parser the doubling code already has
(`AnswerCapture`), extended to keep `input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens` as well as `output_tokens`. It runs for every
recorded request, not only doubled ones, and only ever after the bytes are
written to the client.

`turns` gains `session_id` (from the served request) so side by side can filter.

A new `sessions` table: `id, cwd, branch, first_ts, last_ts`. `first_ts` /
`last_ts` are updated as requests arrive. `cwd` and `branch` are looked up once
per new session id, off the request thread, by globbing
`<claude dir>/projects/*/<id>.jsonl` and reading lines until both fields are
found (stop after ~50 lines). Not found yet (the transcript is written a moment
after the first request) → retry on the session's next request, at most every
30 s.

Label: `basename(cwd) · branch` (branch omitted when empty); no cwd → the first
8 characters of the session id. Requests with no session id show as
"unknown session".

### Prices

- **Valar**: from its `/v1/models` `pricing`, saved alongside the ids in
  `models.json` (so prices survive Valar being down). The price row is the
  **completion window the router sent** (see "Valar completion window" below),
  recorded per request in a `window` column; Valar's response does not echo it
  (`usage.service_tier` is null). Cost = `in × input + cache_read ×
  cached_input + out × output` (per 1M). Valar charges no cache-write fee for
  non-Claude models (docs: "Cache writes"), so cache writes cost 0. A model with
  no price (e.g.
  `valar-auto-balanced`) records `cost = null` and the card shows "partly
  unpriced".
- **Anthropic API prices**: a small table per tier (fable, opus, sonnet, haiku)
  with input, output, cache read and cache write per 1M tokens, kept in a
  `prices.json` in the config folder that the owner can edit, seeded with
  Anthropic's published list prices at build time (look them up; don't guess).
  Tier comes from `tier_of(model_in)`.
- **Local**: 0.
- `api_cost` uses the **same token counts** as the actual request. Valar and
  local models count tokens differently from Claude, so the savings figure is
  an estimate; the savings card says "≈".

### Reading

One query shape serves the page: requests filtered by
`session_id IN (…)` (or all) and `ts` in the chosen span. Per track: count,
share, token sums, `cost` sum, `api_cost` sum. Savings =
Σ over valar+local rows of `api_cost − cost`.

API: the existing `/ui/api/state` stats, `/ui/api/departures`, and the turns
endpoints take optional `sessions=<id,id,…>` and `from=<ts>&to=<ts>`.
New `GET /ui/api/sessions?from&to` lists sessions with a request in the span:
id, label, first/last request time, request count; newest first.

Rule counts: group the filtered rows by the rule that decided (from `reason`).

### Page

Exactly the mockups. Specifics:

- Chips: "All N sessions" first, then one chip per session from
  `/ui/api/sessions` for the chosen span, newest activity first. Click toggles;
  "All" clears the pick. More than ~8 sessions: show the 8 most recent and a
  "+ N more" chip that opens the full list.
- The picked sessions and the time choice are remembered in the browser
  (localStorage) across reloads; if a remembered session is no longer in the
  span it is dropped silently.
- The diagram's caption follows the span: "share of the last hour", "… of the
  last 15 min", "… of the whole session", "… 10:00–12:00".
- Live dots only animate for requests from the picked sessions.
- Cost cards say which span they cover ("· last hour") when it's not "Whole
  session".

## Builder's calls (not discussed; settle sensibly)

- Default time span on first load: **1 hour** (today's behaviour).
- **The time control drives the diagram, the cost cards and the rule counts
  only.** Departures and side by side follow the session pick but stay live
  feeds (today's behaviour: newest first, "Load older"); they ignore the span.
  (my call, from the mockup: departures says "every request from the 2 picked
  sessions", no time)
- **The router fetches Valar's prices itself**, at startup and every hour,
  into `models.json` (the console's model list keeps using the same file).
  Requests recorded before prices are known keep their tokens and get priced
  once prices arrive (a pass over rows with tokens and `cost IS NULL`), so
  "partly unpriced" only stays for models Valar publishes no price for.
  (my call)
- Polling cadence, query indexes (`requests(session_id, ts)` is the obvious one),
  and how the chips refresh as sessions come and go.
- Exact wording of empty states ("No requests from these sessions in the last
  15 min").
- Tests: follow the repo's existing style (unittest, fake upstreams in
  tests/test_proxy.py).

### Valar completion window (probe 2026-10-29)

Valar bills by **completion window**, chosen per request with
`metadata.completion_window` or the `X-Valar-Completion-Window` header (body
wins). With neither, the request runs and bills as **Now** (`asap`), the
"realtime" price row. The router sets neither today.

Measured on Kimi-K3, one tiny streamed request each:

| window | price vs Now | result |
|---|---|---|
| none / `asap` | 100% | 2.0–2.4 s total |
| `priority` | 75% | 12.8 s total (first byte 2.7 s) |
| `standard` | 50% | 11.1 s total (first byte 2.0 s) |
| `flex` | ~33% | 400: no streaming on flex. Unusable for Claude Code |

**Each Valar model slot gets its own window.** On the Valar track row, each of
the four slots (Fable, Opus, Sonnet, Haiku) gets a small window choice under
its model dropdown: `Now · Priority · Standard`, default **Now** (today's
behaviour). The choice only offers windows the chosen model has a price row for
in `/v1/models` (`realtime` = Now, `priority`, `standard`); `flex` and `batch`
are never offered (no streaming). Picking a model that doesn't support the
slot's current window resets the slot to Now.

The router sends the slot's window as `X-Valar-Completion-Window` on every
Valar request that slot serves, served **and** shadow (header, not body, so
the request body is untouched). It records the window on the row and prices by
it. The classifier's own Valar calls always use Now (they have a 2.5 s limit).

Config: a new top-level `windows: {"valar": {fable, opus, sonnet, haiku}}`
(values `asap | priority | standard`); `models` is unchanged. A saved config
without `windows` reads as all `asap`.

(Owner said "per model in Valar"; I read that as **per slot** on the track
row, since that's where models are chosen. If the same model sits in two slots
it can have two windows.)

### Doubled (side-by-side) requests

**They count** ("if it bills, it bills"). Shadow requests record the same token
and cost fields (on `pairs`, per side). The cost cards include them, with the
comparison spend as its own line under the card ("incl. $0.12 comparisons").
The side-by-side cards show each side's cost (mockup v3). Savings counts only
served requests: a comparison isn't something Anthropic would have been paid
for.

## Open points

None.
