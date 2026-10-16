# claude-router — design

Date: 2026-10-16
Status: draft, awaiting approval

## Intent

Amir's workflow is superpowers SDLC in Claude Code: brainstorm → spec →
implementation plan → execute the plan (usually via
`subagent-driven-development`, which dispatches one implementer subagent per
task). He wants the **thinking** (brainstorm, spec, plan, orchestration/review)
to run on Anthropic using the auth he's already logged in with (Claude Max
OAuth today; an API key must also work), and the **typing** (plan execution)
to run on his local model behind LiteLLM.

Running two Claude Code instances and handing the plan file between them was
rejected as sloppy. The chosen shape is a single Claude Code session pointed
at a local router that decides, per request, where the request goes:

```
claude ──ANTHROPIC_BASE_URL──▶ claude-router (127.0.0.1) ──┬─▶ https://api.anthropic.com   (user's auth, untouched)
                                                           └─▶ LiteLLM (:4000) → vLLM      (LiteLLM key, model renamed)
```

openconductor (wilfoa/openconductor) is **not** involved. Anything that
launches `claude` with the env var set — plain terminal, openconductor,
`claude -p` — gets routing for free.

## Goals

- One Claude Code session; zero per-session ritual once installed.
- Routing signals, all supported:
  1. **Prompt tag** in the human message (`#local` / `#plan`).
  2. **Model tier**: opus → Anthropic; sonnet/haiku → local. This also
     gives "main session → Anthropic" (main runs opus) and lets
     superpowers' per-subagent model choice decide each subagent's side.
  (An earlier draft also routed on Claude Code's internal subagent marker;
  dropped because model tier covers it without an undocumented signal.)
- Full fidelity on the Anthropic route: streaming, thinking, prompt caching,
  betas, OAuth — byte-for-byte pass-through except hop-by-hop headers.
- Visible: every routed request logs where it went and why.

## Non-goals

- Cost/token accounting, dashboards, UI.
- Load balancing, retries, caching, or fallback between providers.
- Supporting non-Claude-Code clients.
- openconductor integration.
- Fixing LiteLLM's `0.0.0.0` bind (noted separately; out of scope).

## Anti-goals (failure even if it "works")

- **Anthropic credentials reaching LiteLLM or any local service.** The local
  route must strip `Authorization`/`x-api-key` from the client and inject the
  LiteLLM key instead.
- **Silent rerouting.** If LiteLLM is down, the request fails with a clear
  error; it must never quietly fall back to Anthropic (burns subscription,
  hides the problem), and Anthropic errors never fall back to local.
- Listening on anything but loopback.
- Logging prompt content or secrets.

## Facts established by spike (2026-10-16, Claude Code 2.1.301)

A throwaway pass-through proxy was run with
`ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p ...` including an Agent
tool call:

- Claude Code sends the Max OAuth token as `Authorization: Bearer sk-ant-oat01-…`
  plus `anthropic-beta: claude-code-20250219,oauth-2025-04-20,…` to the custom
  base URL. Forwarding them unchanged to `https://api.anthropic.com` returned
  200 for main and subagent calls. **OAuth pass-through works.**
- Requests: `HEAD /api/hello` (connectivity probe), `POST /v1/messages?beta=true`.
- The `system` field is a list of blocks; the **first block** text is
  `x-anthropic-billing-header: cc_version=…; cc_entrypoint=…;` and, on
  subagent requests only, additionally contains `cc_is_subagent=true;`.
  This is the subagent signal. It is undocumented and may change.
- `model` arrives as the full id Claude Code chose (e.g. `claude-opus-5-5`).
- Not verified: streaming through a proxy (spike buffered), and LiteLLM's
  handling of Claude Code's request shape (79 tools, thinking params, betas).

## Current environment

- LiteLLM: Docker container `litellm`, `http://127.0.0.1:4000`, requires a
  key (`master_key` in `~/litellm/config.yaml`). Serves Anthropic-format
  `/v1/messages`. Model groups `claude-sonnet-4-5-20250929`,
  `claude-opus-4-5`, `claude-haiku-4-5-20251001` all map to the single local
  vLLM model.
- Amir reports the local model already handles Claude Code tool calls well.
- superpowers `subagent-driven-development` requires an explicit `model` on
  every subagent dispatch, chosen by task difficulty (cheap → mechanical
  implementation, standard → integration/review, most capable → design and
  fix-loop escalation). An omitted model inherits the session model (opus).

## Routing rules

Applied to `POST /v1/messages` and `POST /v1/messages/count_tokens`. Every
other path/method (`/api/hello`, OAuth/profile endpoints, etc.) goes to
Anthropic unchanged. A body that fails to parse goes to Anthropic.

First match wins:

1. **Tag.** Find the *latest human text*: walk `messages` backwards, take the
   first `role: "user"` message that has a `text` block which is not a
   `<system-reminder>` block (skip messages consisting only of `tool_result`
   blocks). Match tags as whole words, case-insensitive:
   - `#plan` → Anthropic. `#local` → local.
   - Both present → Anthropic (the safer/smarter side wins).
   Because Claude Code re-sends history, a tag stays in effect for every
   request of that turn (including tool loops) and lapses on the next human
   message. In a subagent, the "human" message is the prompt the parent
   wrote, so the orchestrator (or a plan) can force a subagent either way by
   putting the tag in the task prompt.
   Tags are left in the text (not stripped).
2. **Model tier.** `model` contains `haiku` or `sonnet` → local. Anything
   else (opus, unknown) → Anthropic.

### Main session on local (accepted limitation)

`#local` or `/model sonnet` in the **main** session works, but later
Anthropic requests then carry local-generated assistant turns (e.g. thinking
blocks without valid Anthropic signatures) and Anthropic may reject them,
breaking that conversation. Accepted: the router does no history cleaning.
README documents it: use local in the main session only for throwaway
conversations; tags are mainly for steering subagents. Note that a main
session started on sonnet would go local entirely — README should say to
keep the main session on opus.

## Local route behaviour

- Target: `${LITELLM_URL}/v1/messages` (or `/count_tokens`), same query string.
- Model rewrite: model id containing `haiku` → LiteLLM haiku model;
  everything else (sonnet, or opus forced by `#local`) → LiteLLM sonnet
  model. Both names configurable; defaults are the two LiteLLM groups above.
- Headers: drop `Authorization`, `x-api-key`, hop-by-hop headers; set
  `x-api-key: ${LITELLM_API_KEY}` (or `Authorization: Bearer`, whichever
  LiteLLM accepts — builder verifies). Drop the `oauth-2025-04-20` beta;
  pass other betas through unless LiteLLM rejects them, in which case drop
  the `anthropic-beta` header entirely on this route.
- If LiteLLM rejects Claude Code's request shape (e.g. thinking params), the
  fix belongs in LiteLLM config (`drop_params` etc.), not in body surgery in
  the router — unless that's impossible; then the router may delete the
  specific offending field on the local route only, documented in code.
- LiteLLM unreachable → respond `502` with an Anthropic-shaped error body
  (`{"type":"error","error":{"type":"api_error","message":"claude-router: LiteLLM unreachable at …"}}`).

## Anthropic route behaviour

- Reverse proxy to `https://api.anthropic.com`, path/query/body/headers
  unchanged except hop-by-hop. No body rewriting at all.
- Works for OAuth bearer and `x-api-key` alike (router doesn't care).

## Streaming

Both routes must stream SSE through without buffering (flush each chunk
immediately). Request bodies are read fully (needed for routing) and replayed.

## Logging

One line per proxied request to stderr:
`time route=<anthropic|local> reason=<tag:plan|tag:local|tier|passthrough> model=<in>[→<out>] status=<code> ms=<latency> stream=<bool>`.
Never log bodies, prompts, or credential values.

## Configuration

Environment variables (flags optional, builder's choice):

| Var | Default |
|-----|---------|
| `CLAUDE_ROUTER_LISTEN` | `127.0.0.1:8787` — must resolve to loopback; refuse to start otherwise |
| `CLAUDE_ROUTER_ANTHROPIC_URL` | `https://api.anthropic.com` |
| `LITELLM_URL` | `http://127.0.0.1:4000` |
| `LITELLM_API_KEY` | required; refuse to start if unset |
| `CLAUDE_ROUTER_LOCAL_SONNET` | `claude-sonnet-4-5-20250929` |
| `CLAUDE_ROUTER_LOCAL_HAIKU` | `claude-haiku-4-5-20251001` |

No config file.

## Install / run

- `go install` (or `make install`) into `~/.local/bin/claude-router`.
- systemd **user** unit `claude-router.service`, `Restart=on-failure`,
  key loaded from an `EnvironmentFile` (`~/.config/claude-router/env`,
  mode 0600). Unit file lives in the repo.
- Claude Code: add to `~/.claude/settings.json`
  `"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}`.
- Escape hatch: `ANTHROPIC_BASE_URL= claude` (or removing the setting)
  bypasses the router. README states this.
- Note: if the router is down, every Claude Code session fails to connect.
  That's accepted; README says how to bypass.

## Build decisions (Claude's calls, overridable)

- Go, standard library only (`net/http`, `httputil.ReverseProxy` with
  `FlushInterval: -1`). Single binary. Chosen because openconductor is Go and
  the stdlib proxy streams correctly.
- New repo `~/Development/claude-router`.
- Model tier as the default signal (opus → Anthropic, sonnet/haiku → local);
  local rewrite haiku→haiku, else→sonnet.
- Both-tags → Anthropic.
- 502 on LiteLLM down, no fallback.

## Testing

- Table-driven unit tests for the routing decision: tag in latest human
  message, tag only in an older message (ignored), tag inside a
  system-reminder block (ignored), tool_result-only trailing message, both
  tags, `#plan` on a haiku request, `#local` on an opus request, opus /
  sonnet / haiku / unknown model, unparsable body, non-messages path.
- One integration test with two `httptest` upstreams: verifies streamed
  chunks arrive before the upstream finishes, credentials are swapped on the
  local route and untouched on the Anthropic route, model rewrite applied.
- Manual acceptance (in README): with router + LiteLLM running,
  1. `claude -p "hi"` → log shows `route=anthropic reason=main`.
  2. `claude -p "#local hi"` → `route=local reason=tag:local`.
  3. `claude -p "use a general-purpose subagent with model haiku to say hi"`
     → main on anthropic (tier), subagent on local (tier).
  4. Run a real superpowers plan via `subagent-driven-development` and
     confirm implementers hit local while the orchestrator stays on Anthropic.

## Left to the builder

Package layout, flag parsing, log format details beyond the fields above,
Makefile contents.
