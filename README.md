# claude-router

A loopback HTTP proxy that sits at Claude Code's `ANTHROPIC_BASE_URL`. It sends
each request either to Anthropic (untouched, using your own login) or to a
local LiteLLM (credentials swapped, model renamed). Python 3 standard library
only, in a single file.

```
claude ──ANTHROPIC_BASE_URL──▶ claude-router (127.0.0.1:8787) ──┬─▶ https://api.anthropic.com  (your auth, untouched)
                                                                └─▶ LiteLLM (:4000)             (LiteLLM key, model renamed)
```

## Routing

Applies to `POST /v1/messages` and `/v1/messages/count_tokens`. Everything
else, including any body that can't be parsed, goes to Anthropic unchanged.
The first matching rule wins:

| Signal | Route |
|--------|-------|
| `#plan` in the latest human message (also when `#local` is there) | Anthropic |
| `#local` in the latest human message | local |
| model contains `sonnet` or `haiku` | local |
| anything else (opus, unknown) | Anthropic |

Tags are matched as whole words, case-insensitive. Text inside
`<system-reminder>` and messages that contain only tool results are skipped. A
tag lasts for the whole turn and lapses at the next human message. Inside a
subagent, the "human message" is the prompt the parent wrote, so you can put a
tag there to force where a subagent runs.

On the local route, a model containing `haiku` becomes
`CLAUDE_ROUTER_LOCAL_HAIKU`, and any other model becomes
`CLAUDE_ROUTER_LOCAL_SONNET`. Your `Authorization`/`x-api-key` and the
`oauth-2025-04-20` beta are removed, and `Authorization: Bearer
$LITELLM_API_KEY` is added. If LiteLLM is down, the router returns a 502 and
never falls back to Anthropic.

Each request is logged as one line on stderr:
`time route= reason= model=in[→out] status= ms= stream=`. Bodies, prompts and
keys are never logged.

## Install

```
mkdir -p ~/.config/claude-router
cp env.example ~/.config/claude-router/env
chmod 600 ~/.config/claude-router/env      # then set LITELLM_API_KEY
ln -s $PWD/claude-router.service ~/.config/systemd/user/
systemctl --user enable --now claude-router
```

The router won't start if `LITELLM_API_KEY` is unset or if
`CLAUDE_ROUTER_LISTEN` isn't a loopback address; it exits with code 2.
`LITELLM_API_KEY` is the LiteLLM container's `LITELLM_MASTER_KEY`.
`CLAUDE_ROUTER_LOCAL_SONNET` and `CLAUDE_ROUTER_LOCAL_HAIKU` default to the
live LiteLLM groups; if yours uses different names (`GET /v1/models`), set them.

To turn it on in Claude Code, add this to `~/.claude/settings.json`:

```json
"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}
```

- **Bypass:** `ANTHROPIC_BASE_URL= claude`
- **Router down:** every Claude Code session fails to connect. Use the bypass
  above, or restart the router.
- **Logs:** `journalctl --user -u claude-router -f`

## Main session on local (accepted limitation)

`#local` or `/model sonnet` works in the main session. The catch is that later
requests to Anthropic then carry assistant turns the local model generated,
such as thinking blocks without valid Anthropic signatures, and Anthropic may
reject them, which breaks that conversation. The router does no history
cleaning. **Keep the main session on opus. Use `#local` in the main session
only for throwaway conversations.** Tags are mainly for steering subagents. A
main session started on sonnet runs entirely on local.

## Manual acceptance

Start the router and LiteLLM, then run:

1. `claude -p "hi"`: expect `route=anthropic reason=tier`.
2. `claude -p "#local say hi"`: expect `route=local reason=tag:local`, status 200.
3. `claude -p "use a general-purpose subagent with model haiku to say hi"`:
   expect opus lines on anthropic and haiku lines on local.
4. Run a real superpowers plan with `subagent-driven-development`. The
   implementers should hit local while the orchestrator stays on Anthropic.

### Results (2026-10-16, Claude Code with Max OAuth, LiteLLM → local vLLM gemma-4-26B)

Ran with `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p ...` and the
router on 127.0.0.1:8787.

1. Pass.
   ```
   route=anthropic reason=passthrough model=- status=200 ms=196 stream=False
   route=anthropic reason=tier model=claude-opus-5-5 status=200 ms=1528 stream=True
   ```
2. Pass. Slow, because the local model has to process Claude Code's full prompt.
   ```
   route=local reason=tag:local model=claude-opus-5-5→claude-sonnet-4-6 status=200 ms=123635 stream=True
   ```
3. Pass. The subagent replied "hi".
   ```
   route=anthropic reason=tier model=claude-opus-5-5 status=200 ms=2283 stream=True
   route=local reason=tier model=claude-haiku-5-5→claude-haiku-4-5-20251001 status=200 ms=17634 stream=True
   route=anthropic reason=tier model=claude-opus-5-5 status=200 ms=1461 stream=True
   ```
4. Not run yet; this is the user's to do after enabling.

Notes:
- LiteLLM accepted `Authorization: Bearer`, so no header change was needed.
  It also accepted Claude Code's betas, so `anthropic-beta` is passed through
  without the oauth beta. It already has `drop_params: true`.
- The key is the container's `LITELLM_MASTER_KEY`. The `master_key` in
  `~/litellm/config.yaml` is a placeholder; the live config is
  `config.runtime.yaml`.
- The run used the default model groups (`claude-sonnet-4-6`,
  `claude-haiku-4-5-20251001`). If your LiteLLM uses different group names,
  set `CLAUDE_ROUTER_LOCAL_SONNET` / `CLAUDE_ROUTER_LOCAL_HAIKU`.
