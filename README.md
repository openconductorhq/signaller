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
mkdir -p ~/.config/systemd/user
ln -s $PWD/claude-router.service ~/.config/systemd/user/
systemctl --user enable --now claude-router
```

The unit runs Python with `-E -s` (ignores `PYTHON*` env vars and the user site
directory), so only the standard library is ever loaded.

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

## Console

Open `http://127.0.0.1:8787/ui` (loopback only). It shows live traffic, edits
the routing rules, and compares local and Anthropic answers side by side.

- **Lever:** Anthropic, Rules or Local. Anthropic and Local send everything to
  that track (reason `lever`). Rules uses the Rulebook.
- **Rulebook:** rules are checked in order, first one that decides wins. Nothing
  decided goes to the "Otherwise" track (default Anthropic). Changes apply to
  the next request and are saved to `console.json`.
- **Classifier:** a rule that asks a small LiteLLM model what the newest human
  message is about (labels, each with a track), once per turn and cached. It
  never runs on opus. If it times out or isn't confident, the rule passes and
  the explanation says why. Reason shows as `about:<label>`.
- **Doubling:** sends a copy of a request to the other track and stores both
  answers. Claude Code only ever sees the answer from the track the rules chose.
  Settings are rate (default 0%, so nothing is doubled until you raise it), an
  optional tag (e.g. `both`: only turns with `#both`), and directions (when
  local answers, when Anthropic answers). It picks whole turns: every
  `POST /v1/messages` of a chosen turn is doubled. Copies cost Max quota (to
  Anthropic) and GPU time (to local). At most one local and two Anthropic copies
  run at once; a step that finds its direction full is marked "skipped, busy".
  Judge pairs in Side by side; "Forget all pairs" deletes them.

### Rule language

A rule is one or more lines: `when <condition> go anthropic|local`. The first
matching line decides; no match passes to the next rule.

| Field | Value |
|-------|-------|
| `text` | the latest human message (first 20 000 chars) |
| `tag` | every `#word` in `text` (`tag is plan` means "has this tag") |
| `tier` | `opus`, `sonnet`, `haiku` or `other`, from the model id |
| `model` | the full model id |
| `tokens` | rough size: request bytes / 4 (`100k` means 100 000) |
| `subagent` | yes/no, from `cc_is_subagent=true` in the first system block (undocumented marker, may break) |
| `label` | the classifier's label for this turn, or `none` |

| Operator | Meaning |
|----------|---------|
| `is` | equals, case-insensitive |
| `contains` | case-insensitive substring |
| `matches /re/flags` | Python regex, flags `i` `m` `s` |
| `>` `<` `>=` `<=` | numbers |
| `and` `or` `not`, `( )` | `not` binds tightest, then `and`, then `or` |

```
when tag is plan go anthropic
when tier is sonnet or tier is haiku go local
when text contains "migration" and tier is sonnet go anthropic
when tokens > 120k go anthropic
when not subagent and text matches /^(hi|thanks)\b/i go local
```

Using `label` makes the classifier run for that request (if it's switched on;
otherwise `label` is `none`). The editor previews a rule against your last 200
requests (kept in memory only, so after a restart it covers only newer ones).

### Switching tracks mid-conversation

Same limitation as [Main session on local](#main-session-on-local-accepted-limitation) below, easier to hit now: once a conversation has had
local-generated turns (lever on Local, `#local`, or a rule or the classifier
sending the main session local), sending it back to Anthropic may be rejected.
The router does no history cleaning. If it fails, start a new session. A doubled
copy that Anthropic rejects only shows up as an error on its pair.

### Files

In `~/.config/claude-router/` (override with `CLAUDE_ROUTER_CONFIG_DIR`), mode 0600:

- `console.json`: the routing config. If it's unreadable at startup, the router
  uses defaults, keeps the bad file as `console.json.bad` and shows a banner.
- `stats.db`: SQLite stats. It stores the first 120 characters of each message
  and the doubled answers (text and tool calls), locally. Never headers, keys
  or full bodies. Requests are pruned after 30 days; pairs and verdicts stay
  until "Forget all pairs".

### Console acceptance

1. Lever to Local, `claude -p "hi"` goes local (see the Departures feed); lever
   back to Rules.
2. Add the rule `when text contains "zebra" go local`. The preview shows
   matches. `claude -p "zebra"` goes local.
3. With the classifier on against the real LiteLLM, a haiku subagent prompt
   "fix the failing test" shows `about: coding` (or a timeout note) in its
   explanation.
4. Doubling rate 100%, tag `both`: `claude -p "#both use a haiku subagent to say hi"`
   puts a turn in Side by side, and a verdict saves.
5. With reduced motion set in the OS, there are no moving dots.

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
