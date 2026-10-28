# claude-router: Valar as a third track — design

Date: 2026-10-28
Status: draft, awaiting approval
Builds on: `2026-10-17-console-design.md` (branch `feat/console`, PR #2) and
`2026-10-16-claude-router-design.md`. Everything there holds unless this
document changes it.

Mockup: `2026-10-28-valar-track/cockpit-valar.html` (only the parts that
change; same look as `cockpit-v2.html`).

## Intent

Add **Valar** (`api.valarhq.ai`, hosted open-weight models behind an
Anthropic-compatible API) as a third track next to Anthropic and local
LiteLLM. Amir's routing:

- fable/opus → Anthropic; every other model → Valar
- `#planning` → Anthropic, `#execution` → Valar, `#local` → LiteLLM
- which Valar (or LiteLLM) model serves a Claude Code sonnet/haiku request is
  chosen on the page, from the models the upstream serves

## Facts (probe 2026-10-28)

- Valar is its own server, not LiteLLM. `GET /v1/models` lists ~22 models
  (DeepSeek V4/V4.1, GLM-5.x, Kimi K3, Qwen 3.x, MiniMax, gpt-oss,
  `valar-auto-balanced`), all with tools.
- `POST /v1/messages` and `/v1/messages/count_tokens` speak the Anthropic
  Messages API (documented: docs.valarhq.ai/anthropic-sdk). Base URL has
  **no** `/v1` (the client appends it). Key as `Authorization: Bearer` or
  `x-api-key`. SSE streaming works.
- A Claude Code-shaped request (claude-code / interleaved-thinking /
  context-management betas, `thinking`, `cache_control`, billing-header
  system block, tools) → 200 with a correct `tool_use`. No body surgery.
- `thinking.budget_tokens` is ignored (enabled = medium effort). Thinking
  blocks come back with `signature: ""`, so Anthropic will reject them if
  replayed: the existing "switching tracks mid-conversation" limitation
  applies to Valar exactly as to local.
- `POST /v1/chat/completions` (OpenAI format) also works; the classifier
  uses it.
- Valar's own ValarCode / on-device proxy is **rejected**: it rewrites
  Claude Code settings, may wrap the `claude` binary, installs a MITM
  certificate, and its quota-first mode silently falls back to Valar.

## Changes

### Tracks

`TRACKS = ("anthropic", "valar", "local")` everywhere a track is named:
rule language (`go valar`), "Otherwise", classifier label tracks, lever,
stats, departures, doubling, verdicts. Valar's colour is blue `#2F6DB5`
(Anthropic amber, local green, shadow violet, error red unchanged).

### Tier

`tier_of` learns `fable`. Tier values: `opus`, `fable`, `sonnet`, `haiku`,
`other` (anything else, including non-Claude ids like `zai-org/GLM-5.3`).
The classifier rule skips `fable` as it skips `opus` (main session).

### Default rulebook (fresh config only)

1. **Tags in your message**
   ```
   when tag is planning go anthropic
   when tag is local go local
   when tag is execution go valar
   ```
   (`#plan` is no longer a tag; precedence planning > local > execution
   comes from line order.)
2. **What the prompt is about**: labels planning, design & review →
   anthropic; coding, chit-chat → valar.
3. **Model the request asked for**
   ```
   when tier is opus or tier is fable go anthropic
   when tier is sonnet or tier is haiku or tier is other go valar
   ```

Otherwise → Anthropic (unchanged). A saved config keeps its own rules; the
rulebook is user data and isn't rewritten.

### Track models (new)

`console.json` gains:

```json
"models": {"valar": {"sonnet": "zai-org/GLM-5.3", "haiku": "deepseek-ai/DeepSeek-V4.1-Flash-1017"},
           "local": {"sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"}}
```

On the valar/local track, a `claude-*` model with `haiku` in it becomes that
track's `haiku` model; any other `claude-*` model (sonnet, or opus/fable
forced there by a tag or the lever) becomes its `sonnet` model. Non-Claude
ids pass through unchanged, so `/model zai-org/GLM-5.3` picks a Valar model
directly. Both strings must be non-empty. The `CLAUDE_ROUTER_LOCAL_SONNET` /
`_HAIKU` env vars are retired (README says so); their defaults move here.

### Doubling

"Send a copy to the other track" becomes a choice per served track:

```json
"doubling": {"rate": 0, "tag": "", "shadow": {"anthropic": "valar", "valar": "anthropic", "local": null}}
```

`null` = don't double requests served there. A shadow track equal to its
served track is invalid. Concurrency caps: anthropic 2, valar 2, local 1.
Verdicts become a track name or `same`. The tally counts per track.

### Classifier upstream (Amir's choice: selectable, default Valar)

`classifier` gains `"upstream": "valar" | "local"` (default `valar`) and its
default model becomes `zai-org/GLM-5.3-Flash`. The classifier calls
`<upstream>/v1/chat/completions` with that upstream's key. The cache key
includes the upstream. An unconfigured upstream makes the classifier stay
silent with the note "classifier: Valar not configured" (the anti-goal
stands: classifier trouble never picks a track). Prompt text goes to Valar
for classification; it would go there anyway on the Valar track.

If Valar's reasoning models put their answer somewhere the classifier's
parser doesn't read, the builder fixes the parser (live acceptance checks
it), not the model choice.

### Config file version

`version: 2`. A version-1 file loads by migration: add `models` defaults,
`classifier.upstream = "local"` (keeps its old behaviour), doubling
`shadow = {"anthropic": "local" if when_anthropic else null, "local":
"anthropic" if when_local else null, "valar": null}`. The next save writes
version 2. Anything else that fails validation keeps the existing rule:
the router starts on defaults with a banner.

### Upstream configuration (env)

| Var | Default |
|-----|---------|
| `VALAR_URL` | `https://api.valarhq.ai` (a trailing `/` or `/v1` is stripped; the repo `.env` has `/v1`) |
| `VALAR_API_KEY` | optional |
| `LITELLM_URL` | `http://127.0.0.1:4000` |
| `LITELLM_API_KEY` | **now optional** (was required) |

A track whose key is unset answers `502` with
`claude-router: Valar not configured (VALAR_API_KEY unset)` (or LiteLLM). It
never falls back to another track. Valar and LiteLLM share one code path:
strip client `Authorization`/`x-api-key`, drop the `oauth-2025-04-20` beta,
inject `Authorization: Bearer <track key>`, rewrite the model, stream back.
The origin/host guard that protects the injected LiteLLM key now protects
both keys.

### The page

- **Lever**: four positions: *Anthropic* · *Rules* · *Valar* · *Local*. The
  "may fail when you switch back" warning shows for Valar and Local.
- **Yard**: three tracks (Anthropic top, Valar middle, Local bottom), each
  with last-hour share and its model (the `sonnet` model for valar/local).
  An unconfigured track is drawn dashed with "not configured".
- **Tracks** (new panel, under the yard): one row per non-Anthropic track:
  URL, key status (`key set` / `no key`), and two model fields, "Sonnet &
  bigger" and "Haiku". Each is an input with suggestions from
  `GET /ui/api/models?track=valar|local`: the router fetches
  `<upstream>/v1/models` with the track's key and returns the sorted ids.
  The key never reaches the browser. Free text is allowed. Failure shows
  "Couldn't load models (<reason>). Type a model id." Saving goes through
  the existing config PUT.
- **Rulebook**: "Otherwise" and label chips get a third option; the rule
  editor highlights `valar` as a track word. The classifier card gains an
  upstream select (Valar / LiteLLM) next to its model.
- **Side by side**: the strip's two checkboxes become three rows, "When
  <track> answers, compare with [select: off / other tracks]".

### Logging and stats

`route=valar` in the stderr log. Stats: `anthropic_pct`, `valar_pct`,
`local_pct`; tally `{anthropic, valar, local, same}`.

## Non-goals

- ValarCode (see Facts).
- Valar's completion-window header (`X-Valar-Completion-Window`). Easy to
  add later as a per-track setting.
- More than three tracks, or user-defined upstreams.
- Migrating a saved rulebook to the new tag names.

## Testing

TDD per task, as in the console plan. Must-haves:

- Routing: `go valar` parses and evaluates; `tier is fable`; the default
  rulebook gives every row of the Intent table; the classifier skips fable.
- Config: v1 file → v2 migration (models, upstream, shadow); v2 round-trip;
  invalid `models` / `shadow` / `upstream` rejected.
- Proxy: Valar track swaps credentials (Bearer VK, no client key, no oauth
  beta), rewrites the model per `models.valar`, streams; non-Claude ids are
  untouched; unset key → 502 "not configured"; no fallback.
- Models API: returns sorted ids; key sent upstream and not in the response;
  guarded like the rest of `/ui/api`.
- Classifier: upstream selection uses the right URL and key; an
  unconfigured upstream stays silent.
- Doubling: shadow per served track; `null` = off; caps include valar.
- Page: the existing UI tests extended to three tracks.
- Live acceptance against real Valar (key from repo `.env`):
  `claude -p hi` → anthropic; `#execution hi` → valar with the mapped model;
  a haiku subagent → valar haiku model; classifier on Valar returns a
  label; `#local` → 502 "LiteLLM unreachable" on this Mac (no LiteLLM),
  never falling back.
