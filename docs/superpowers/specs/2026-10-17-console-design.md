# signaller console — design

Date: 2026-10-17
Status: draft, awaiting approval (builder check done)
Builds on: `2026-10-16-signaller-design.md` (the router, PR #1, branch
`feat/router`). Everything there still holds unless this document changes it.
This document replaces that spec's "no dashboards / UI" non-goal and its
"no config file" rule.

Mockups (open in a browser; they are fragments, unstyled outside the
brainstorm frame but readable): `2026-10-17-console/cockpit-v2.html` (the
whole page) and `2026-10-17-console/rule-editor.html` (writing a rule). The
look in them is approved; treat them as the visual reference, not as code to
reuse.

## Intent

The router decides per request whether Claude Code talks to Anthropic or to
the local model. Today that logic is fixed in code and visible only in a log.
Amir wants to **see** where traffic goes and **change the routing while it
runs**, from one page:

- see routing stats and every request with the reason it went where it did;
- flip a global override ("everything local now", "everything Anthropic
  now", "follow the rules");
- route by **what the prompt is about**, decided by a small model;
- write **his own rules** in a small rule language (first ones will be
  term-based and prompt-size-based; more kinds will follow, so adding a new
  thing a rule can look at must be cheap);
- **double** requests: send a copy to the other track and compare the two
  answers side by side, to learn whether local is good enough.

It's a personal local tool. It should feel delightful, not like an admin
panel.

## Goals

- One page, the "cockpit", at `http://127.0.0.1:8787/ui`, served by the
  router itself. Nothing extra to install or start.
- Every change made on the page applies to the **next request**, with no
  restart, and survives a router restart.
- The router stays fast: it adds **under 5 ms per request** (median, see
  Speed) not counting the prompt classifier.
- Nothing the console does (stats, classifier, doubling, the page itself)
  can break or slow the request Claude Code is waiting for.

## Non-goals

- Remote access, multiple users, login. It is a loopback page for one person.
- Cost or token accounting beyond what's listed under Stats.
- Learning from verdicts. Verdicts on doubled pairs are for Amir to read;
  nothing auto-tunes rules from them.
- Editing LiteLLM's config, or managing models.
- Dark theme (light only, as mocked).
- Cleaning conversation history so a session can move between tracks (see
  "Switching tracks mid-conversation").

## Anti-goals (failure even if it "works")

- The page or its API reachable from anything but loopback, or callable from
  another website open in the browser.
- The page loading anything from the internet (fonts, scripts, CDNs). Fonts
  ship in the repo. Privacy rule: localhost only.
- A doubled ("shadow") answer ever reaching Claude Code, or a shadow failure
  changing the served request in any way.
- Classifier trouble (slow, down, unsure, garbage) changing a request's
  track by itself. It may only stay silent, letting the next rule decide.
- Silent rerouting: the v1 rule stands. If the chosen track fails, the
  request fails; it never falls back to the other track.
- Credentials (headers, keys, tokens) written to disk or shown on the page.
- The stderr log gaining prompt content. (Prompt content does go to the
  local stats database now; see Stats.)

## The page

One screen, top to bottom, as in `cockpit-v2.html`. Railway-junction
metaphor: requests are trains, Anthropic and local are the two tracks, the
override is the signal-box lever.

Palette: paper `#EEF1EC` background, ink `#1C2B36`, muted `#5E6B73`, rail
`#B9C2BD`, Anthropic amber `#D8901E`, local green `#1F8A5B`, shadow violet
`#6B5BD6`, error red `#C8412F`. Type: Barlow Condensed (headings, numbers,
lever) and IBM Plex Sans (everything else), both served from the repo as
woff2 (both are OFL). Plex Mono for the rule editor.

### Header and lever

Title, `127.0.0.1:8787 · up <uptime>`, and the **lever**: three positions,
*Anthropic* ("everything, ignore rules"), *Rules* ("follow the rulebook"),
*Local* ("everything, ignore rules"). A radiogroup: click or arrow keys.
When the lever is not on Rules, the page makes that obvious (e.g. the
Rulebook dims with a line "The lever is sending everything to <track>").
The lever position persists across restarts.

### The yard (live traffic)

SVG junction: a grey line from "Claude Code" into a junction, splitting to
the amber Anthropic track and the green local track, each with its share of
requests over the **last hour** as a big number and the upstream model name.
Under "Claude Code": requests per minute (last 5 minutes).

Each real request animates one dot along the track it took, as it happens.
A doubled request also sends a faded violet dot down the other track. When
nothing is happening, nothing moves. With `prefers-reduced-motion`, no dots
move; the numbers still update.

### Rulebook

An ordered list, checked top to bottom; the first rule that decides wins.
Each rule has a toggle (on/off), can be dragged to a new position (and moved
with keyboard: focused rule + Alt+↑/↓), and can be edited or deleted (built-in
rules can be edited and switched off, not deleted). A fixed last line reads
"Otherwise → Anthropic" with the track selectable (default Anthropic, as in
v1).

Shipped defaults, in order:

1. **Tags in your message**: `#plan` → Anthropic, `#local` → local.
2. **What the prompt is about**: the classifier (below), shown with its
   label → track chips and its confidence slider.
3. **Model the request asked for**: opus → Anthropic, sonnet and haiku →
   local.

Rules 1 and 3 are ordinary rules written in the rule language (below),
shown with a friendly name and description; "edit" reveals their rule text.
Rule 2 is the one special kind. With these defaults and the lever on Rules,
behaviour equals v1 plus the classifier. (v1's "both tags → Anthropic" holds
because the `#plan` rule is checked before `#local`.)

"Add rule" opens the rule editor (below).

### Departures (request feed)

Every `POST /v1/messages` request, newest first (Claude's call:
`count_tokens`, passthrough and forbidden requests are not shown, not
counted in the stats, and not stored; they still go to the stderr log and
still follow the routing): time, a coloured dot for the track (red
for errors), the first words of the newest human message (one line,
ellipsised), and a short reason (`tag`, `about: coding`, `model: haiku`,
`your rule: <name>`, `lever`, `LiteLLM down · 502`). New rows appear live.
Shows the last 200; scrolling further loads older ones from the database.

Opening a row shows how it was decided, rule by rule, in order: what each
rule said ("none found", "coding, 88% sure → local", "not reached",
"classifier timed out after 1.5 s", "switched off"), plus model in → out,
status, time to first byte and total time, and, if doubled, the other
track's status and time with a link to the pair.

### Side by side (doubled pairs)

The doubling settings strip, then the latest doubled **turn**: its first
words, and its steps in order (step N of M, prev/next; skipped steps
shown as such). Each step shows two answer cards (local
green-topped, Anthropic amber-topped) each with track, time, output tokens,
and the answer; the served one is badged "sent to Claude Code". Answers show
their text and any tool calls (tool name + input, compact). Below the turn: "Which
did better on this turn? Local / Anthropic / About the same" (one verdict
per turn, Claude's call) and a running tally. Prev / next through turns,
with "unjudged first" as the default order. "Forget all
pairs" (with a confirm) deletes stored pairs.

## Routing, as changed

Order of decision for each request on a routed path (`POST /v1/messages`,
`POST /v1/messages/count_tokens`; everything else passes through to
Anthropic exactly as in v1, except `/ui` which the router now serves):

1. **Lever** on Anthropic or Local → that track. Reason `lever`.
2. Otherwise, rules in order, enabled ones only. First rule that decides wins.
3. Nothing decided → the "Otherwise" track. Reason `default`.

The routing config (lever, rules, classifier settings, doubling settings) is
one immutable object. The page edits it by sending a whole new config to the
router, which validates it, writes it to disk, then swaps it in. A request
uses the config that was current when it arrived. An invalid config is
rejected with a message saying what's wrong and changes nothing.

Stderr log line from v1 stays, with `reason=` values extended to: `lever`,
`tag` (v1's `tag:plan`/`tag:local` become `rule:<rule name>` like any
other rule), `rule:<name>`, `about:<label>`, `default`, `passthrough`,
`forbidden`. Add `shadow=<track>|-` to the line.

## The rule language

A rule is a name, an enabled flag, and one or more lines, each:

```
when <condition> go anthropic|local
```

Lines are checked in order; the first line that matches decides for the
rule. If none matches, the rule passes to the next rule. (Claude's call:
lets built-ins like "Tags" hold `#plan` → Anthropic and `#local` → local in
one row, with `#plan` first so "both tags → Anthropic" holds. Reasons and
explanations name the rule, plus the line number when a rule has several.)

Conditions compare **fields** of the request:

| Field | Type | Value |
|-------|------|-------|
| `text` | text | the latest human message (v1's `latest_human_text`), capped at 20 000 chars |
| `tag` | tag set | every `#word` in `text` (whole word, case-insensitive) |
| `tier` | word | `opus`, `sonnet`, `haiku`, or `other`, from the model id |
| `model` | text | the full model id as requested |
| `tokens` | number | rough request size: request body bytes ÷ 4 |
| `subagent` | yes/no | `cc_is_subagent=true` present in the first system block (undocumented Claude Code marker; may break) |
| `label` | word | the classifier's label for this turn, or `none` |

Operators: `is` (equals, case-insensitive; for `tag`, "has this tag"),
`contains` (case-insensitive substring), `matches /regex/flags` (Python
`re`, flags `i` `m` `s`), `>` `<` `>=` `<=` (numbers; `k` suffix means
×1000, so `tokens > 100k`). Join with `and`, `or`, `not`, parentheses;
`not` binds tightest, then `and`, then `or`. Text values in double quotes;
bare words allowed for single-word values (`tier is sonnet`, `tag is plan`).

Examples:

```
when tag is plan go anthropic
when tier is sonnet or tier is haiku go local
when text contains "migration" and tier is sonnet go anthropic
when tokens > 120k go anthropic
when not subagent and text matches /^(hi|thanks)\b/i go local
```

Requirements:

- Parsed by a small hand-written parser into a tree; never `eval`.
- Adding a field is one table entry (name, type, how to read it from the
  request). This is the extension point for "more to follow".
- Referencing `label` makes the classifier run for that request if it
  hasn't for this turn (subject to its own on/off switch; if off,
  `label` is `none`).
- Regex: compiled once at save; evaluated only on the capped `text`.
  `# ponytail: no regex timeout in Python re; a pathological pattern could stall a request — the editor's preview (below) surfaces slow patterns; add a guard if it bites.`

### Rule editor

As in `rule-editor.html`: a name field, a monospace editor for the
statement, and live feedback as you type (debounced):

- parse errors inline, pointing at the spot ("expected `go` after the
  condition");
- once valid: "In your last 200 requests this would have matched N, and
  changed the track for M of them", with a table of the matched requests
  (time, where it went and why, where it would go, first words). "Would go"
  means: evaluated at this rule's position in the current Rulebook.
- if evaluating over the 200 took over 50 ms, a warning that the rule is
  slow.

The preview needs the fields of recent requests. The router keeps the
fields of the last 200 routed requests **in memory** (not on disk); after a
restart the preview covers only requests since then, and says so.
`label` in the preview is whatever the classifier said at the time (no
re-classifying).

## The prompt classifier

A rule kind that asks a small model what the newest human message is about.

- **Model**: a LiteLLM model group, through the same LiteLLM URL and key as
  the local route. Default `gemma-haiku` (today every group is the same
  gemma-4-26B; a smaller model can be added to LiteLLM later and picked
  here). Editable on the page.
- **Labels**: an editable list; each has a name, a one-line description
  (fed to the model), and a track. Defaults: *planning* (designing,
  specifying, planning work) → Anthropic; *design & review* (judging,
  reviewing, critiquing code or ideas) → Anthropic; *coding* (writing,
  editing, fixing, running code) → local; *chit-chat* (greetings, thanks,
  small talk) → local.
- **Call**: one non-streaming request with a short fixed instruction listing
  the labels, plus the newest human message only (capped, e.g. 4 000 chars).
  The model answers JSON `{"label": "...", "confidence": 0-1}`. Confidence is
  the model's own estimate. Unknown label or unparsable answer = no answer.
- **Once per turn**: results are cached in memory by the hash of the
  newest human message text (LRU, a few hundred entries). Every request of
  the same turn (tool loops, count_tokens) reuses it.
- **Time limit**: default 1.5 s, editable. Past it, the classifier says
  nothing for this request; the call is abandoned. (The result of a late
  call is still cached if it arrives, so the next request of the turn
  benefits.)
- **Decides only** when it answered, the label maps to a track, and
  confidence ≥ the slider (default 70%). Otherwise it passes to the next
  rule. Its outcome (label, confidence, or "timed out" / "error: …" /
  "below 70%") goes into the request's explanation.
- **Never moves the main session** (Amir's decision): the classifier only
  decides for requests whose `tier` is not `opus`. On opus requests it is
  skipped without a call, the explanation says "skipped: main session
  (opus)", and the request falls to the next rule (by default the model
  rule → Anthropic). So with the shipped defaults the main session always
  stays on Anthropic, as in v1. The classifier is on by default.
- Classifier calls are not shown in Departures and not doubled.

## Doubling

Send a copy of a request to the other track, keep both answers, compare.
Claude Code always gets the answer from the track the rules (or lever)
chose; the copy is only stored.

Settings (the strip in "Side by side"):

- **Doubling picks whole turns** (Amir's decision). A turn is everything
  sent for one human message: the decision is made on its first request
  (keyed by the hash of the newest human message, like the classifier
  cache) and then every `POST /v1/messages` request of that turn is
  doubled. In a subagent, the "human message" is the task prompt the parent
  wrote, so a doubled subagent run is compared step by step.
- **Rate** 0–100% of turns, chosen at random. 100% = every candidate turn.
- **Tag** (optional, a word you choose, e.g. `both`). If set, only requests
  whose newest human message has `#<tag>` are candidates. Empty = every
  request is a candidate. Like `#plan`, the tag covers the whole turn and
  stays in the text.
- **Directions**: "when local answers" and "when Anthropic answers", two
  checkboxes, both on by default.
- Default rate is **0%** (Claude's call): a fresh install doubles nothing
  until you turn it up, so it never spends quota or GPU unasked.

So "1 in 5 with `#both`" = rate 20% + tag `both` (random, about 1 in 5).

Behaviour:

- Only `POST /v1/messages` is doubled (not count_tokens).
- The shadow is sent in the background as the served request starts. It
  never delays the served response, and its failure is recorded on the
  pair only.
- Shadow to Anthropic: same body and the incoming request's own headers
  (Claude Code's OAuth/API key, held in memory only). Shadow to local: the
  local-route rewrite (model rename, credential swap) from v1.
- The builder may set `stream: false` on the shadow body for simplicity;
  the served request is never altered.
- At most **one local shadow and two Anthropic shadows in flight**. A step
  of a doubled turn that finds its direction full is skipped and marked
  "skipped, busy" in that turn (the GPU is shared with real local work;
  local copies of fast Anthropic steps will often hit this).
- The served answer is captured for pairs by reading the SSE events as they
  pass through (text deltas, tool_use blocks, output token usage), without
  holding back any bytes.
- A pair stores both answers, each one's status, time to first byte, total
  time, output tokens, plus the verdict.
- Shadow answers are never acted on: if the copy "wants" to edit a file,
  nothing happens. Each pair compares one step; later steps on the copy's
  side are still answers to the real conversation.

## Switching tracks mid-conversation

v1's accepted limitation is still true and the console makes it easier to
hit: once a conversation has had local-generated turns (lever on Local,
`#local`, a classifier or rule sending the **main** session local), sending
that conversation back to Anthropic may be rejected (local thinking blocks
lack Anthropic signatures). The router still does no history cleaning.

The page warns where it matters:
- moving the lever to Local: "Conversations that run on local may fail when
  you switch back. Start a new session afterwards if they do."
- in the Rulebook, a rule that can send opus (main-session) requests local
  gets a small warning badge. Simplest check: a rule whose condition doesn't
  require `tier` other than opus / `subagent`. Builder's call on how
  precise.

Shadow copies of such conversations to Anthropic may also be rejected; that
lands on the pair as an error, nothing more.

## Stats and storage

Two files under `~/.config/signaller/` (mode 0600, created on first
run):

- `console.json`: the routing config (lever, rules, classifier, doubling,
  "otherwise" track). Written atomically (temp file + rename) on every
  change. Read at startup; if missing, defaults are used (which reproduce v1
  + classifier). If unreadable/invalid at startup: log it, start with
  defaults, keep the bad file aside as `console.json.bad`, and show a
  banner on the page. Hand-editing is possible but the page is the editor;
  the file isn't watched.
- `stats.db`: SQLite (stdlib `sqlite3`).
  - `requests`: time, track, reason, the explanation (per-rule outcomes as
    JSON), model in/out, status, time to first byte, total time, stream
    flag, first 120 chars of the newest human message, pair id if doubled.
    Pruned after 30 days.
  - `pairs`: turn id, request id, step number, served track, shadow
    track, each side's answer (text + tool calls), status (incl. skipped,
    busy), timings, output tokens.
  - `turns`: turn id, first 120 chars of the message, time, verdict. Kept until
    "Forget all pairs".
  - Never: headers, credentials, full request bodies.

All database writes go through one background writer thread fed by a
queue; request threads never wait on SQLite. If the queue backs up (e.g. a
stuck disk), drop stats rows and count drops rather than block.

Numbers on the page: last-hour split per track, requests per minute (last 5
minutes), today's doubled turns and judged count, verdict tally, and the
error count in the last hour shown in red when non-zero.

## The page's plumbing

- Served by the existing `ThreadingHTTPServer` under `/ui` (the page and
  its static files: one HTML, one JS, one CSS, fonts) and `/ui/api/...`
  (JSON). No build step, no npm, no framework: plain HTML/CSS/JS. Native
  HTML drag-and-drop for reordering.
- Live updates by polling `/ui/api/...` every second with a "since id"
  cursor (cheap on loopback). The builder may use SSE instead if it's
  simpler.
- API (builder may reshape): get state (config, stats numbers), get
  departures (since / before), get one request's explanation, put config
  (validate + save + swap), preview rule, get pairs / set verdict / forget
  pairs.

### Security

v1 rejects any request carrying an `Origin` header and any non-loopback
`Host`. For `/ui` and `/ui/api/*` only:

- `Host` must be loopback (unchanged).
- A request with an `Origin` header is allowed only if the Origin is exactly
  this router's own (`http://127.0.0.1:<port>` or `http://localhost:<port>`,
  matching the Host). Anything else: 403.
- State-changing API calls (PUT/POST/DELETE) **require** a matching Origin
  header (browsers always send one on such fetches), so a cross-site form
  post, which carries a foreign Origin, is refused.
- Responses set `Content-Security-Policy: default-src 'self'`,
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`.
- No login token (Claude's call): Host + Origin checks stop other websites;
  local processes can already read `console.json` and reach LiteLLM, so a
  token would add nothing against them.

`/v1/*` keeps v1's rule (any Origin → 403).

## Speed

- Budget: the router adds **< 5 ms** median per request versus calling the
  upstream directly, with the default rules plus 20 user rules, classifier
  answer cached, stats on. Measured by a test against a local fake upstream
  (e.g. 200 requests; the test fails over budget).
- The classifier is the only thing allowed to add more, and only up to its
  time limit, once per turn.
- Stays Python 3.12 stdlib (Claude's call: the router's own time is ~1 ms
  against seconds to minutes upstream; Rust isn't installed and would mean
  rewriting a tested router). If the budget test can't be met in Python,
  revisit.

## Testing

On top of v1's tests (all must keep passing):

- Rule language: parse table (valid statements, each operator, precedence,
  `k` suffix, quoting, bare words) and error table (message + position);
  evaluation table against hand-built field sets; unknown field rejected at
  save.
- Defaults reproduce v1 decisions for every v1 routing test case with the
  classifier off, and for every opus case with it on (it never calls on
  opus).
- Engine: lever overrides; first-match order; disabled rules skipped;
  "otherwise" track; explanation lists every rule's outcome.
- Classifier against a fake LiteLLM: decides at/above threshold, passes
  below; timeout passes and the late answer is cached; bad JSON / unknown
  label passes; one call per turn across several requests.
- Doubling against fake upstreams: per-turn selection (all requests of a
  chosen turn doubled, none of an unchosen one), rate/tag/direction; served
  response byte-identical and not delayed by a slow or failing shadow;
  concurrency caps skip and count; pair stored with both answers.
- Config: save → next request uses it; invalid config rejected unchanged;
  restart reloads; corrupt file → defaults + `.bad` + banner.
- Security: `/ui/api` PUT without Origin → 403, foreign Origin → 403,
  own Origin → OK; `/v1/messages` with own Origin still 403; non-loopback
  Host → 403.
- Stats writer: a blocked database never blocks a request.
- Speed budget test (above).
- Page: manual acceptance in a browser (lever, add/edit/reorder rules,
  departures explanations, pair verdicts, reduced-motion), listed in README.

## Left to the builder

Module layout (the single module will need splitting), API shapes, polling
vs SSE, exact JSON schema of `console.json` (include a version number),
how precise the "can send main session local" warning is, `stream:false`
on shadows, classifier prompt wording, the page's exact layout below the
mocked level of detail.
