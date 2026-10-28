# claude-router

A loopback HTTP proxy that sits at Claude Code's `ANTHROPIC_BASE_URL`. It sends
each request to one of three tracks: Anthropic (untouched, using your own
login), Valar (hosted, credentials swapped, model renamed) or a local LiteLLM
(credentials swapped, model renamed). Python 3 standard library
only, in a single file.

```
claude ──ANTHROPIC_BASE_URL──▶ claude-router (127.0.0.1:8787) ──┬─▶ https://api.anthropic.com  (your auth, untouched)
                                                                ├─▶ https://api.valarhq.ai      (Valar key, model renamed)
                                                                └─▶ LiteLLM (:4000)             (LiteLLM key, model renamed)
```

## Routing

Applies to `POST /v1/messages` and `/v1/messages/count_tokens`. Everything
else, including any body that can't be parsed, goes to Anthropic unchanged.
The console's rulebook decides; a fresh config ships this default (first
match wins):

| Signal | Track |
|--------|-------|
| tag `#planning` | Anthropic |
| tag `#local` | local |
| tag `#execution` | Valar |
| tier opus or fable | Anthropic |
| tier sonnet, haiku or other | Valar |
| classifier label planning, design & review | Anthropic |
| classifier label coding, chit-chat | Valar |
| otherwise | Anthropic |

Tags are matched as whole words, case-insensitive. Text inside
`<system-reminder>` and messages that contain only tool results are skipped. A
tag lasts for the whole turn and lapses at the next human message. Inside a
subagent, the "human message" is the prompt the parent wrote, so you can put a
tag there to force where a subagent runs.

On the Valar route, `sonnet` becomes `zai-org/GLM-5.3` and `haiku` becomes
`deepseek-ai/DeepSeek-V4.1-Flash-1017`; on the local route they become
`claude-sonnet-4-6` and `claude-haiku-4-5-20251001`. These maps are edited on
the console page (Tracks panel), not by env vars. Your
`Authorization`/`x-api-key` and the `oauth-2025-04-20` beta are removed, and the
track's key is added (`VALAR_API_KEY` or `LITELLM_API_KEY`). A track whose key
is unset, or that is down, answers 502 and never falls back to another track.

Each request is logged as one line on stderr:
`time route= reason= model=in[→out] status= ms= stream=`. Bodies, prompts and
keys are never logged.

## Install

```
mkdir -p ~/.config/claude-router
cp env.example ~/.config/claude-router/env
chmod 600 ~/.config/claude-router/env      # then set VALAR_API_KEY (and LITELLM_API_KEY if you use local)
mkdir -p ~/.config/systemd/user
ln -s $PWD/claude-router.service ~/.config/systemd/user/
systemctl --user enable --now claude-router
```

The unit runs Python with `-E -s` (ignores `PYTHON*` env vars and the user site
directory), so only the standard library is ever loaded.

The router won't start if `CLAUDE_ROUTER_LISTEN` isn't a loopback address; it
exits with code 2. `VALAR_URL` defaults to `https://api.valarhq.ai` (a trailing
`/` or `/v1` is stripped). `VALAR_API_KEY` and `LITELLM_API_KEY` are both
optional; a request to a track without its key gets a 502. `LITELLM_API_KEY` is
the LiteLLM container's `LITELLM_MASTER_KEY`. `CLAUDE_ROUTER_LOCAL_SONNET` and
`CLAUDE_ROUTER_LOCAL_HAIKU` are retired: model names now live on the console
page and in `console.json`.

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
the routing rules, and compares track answers side by side.

- **Lever:** Anthropic, Rules, Valar or Local. Anthropic, Valar and Local send
  everything to that track (reason `lever`). Rules uses the Rulebook.
- **Tracks panel:** per-track state (key set or not) and the model maps for
  Valar and local (sonnet, haiku), plus the classifier's upstream and model.
  Edits apply to the next request and are saved to `console.json`.
- **Rulebook:** rules are checked in order, first one that decides wins. Nothing
  decided goes to the "Otherwise" track (default Anthropic). Changes apply to
  the next request and are saved to `console.json`.
- **Classifier:** a rule that asks a small model (Valar by default, or LiteLLM) what the newest human
  message is about (labels, each with a track), once per turn and cached. **Privacy:** by default the
  classifier sends the newest human message's text to Valar
  (`deepseek-ai/DeepSeek-V4.1-Flash`, 2.5 s timeout, reasoning off). Switch its
  upstream to local to keep that text on your machine. It never runs on opus. If it times out or isn't confident, the rule passes and
  the explanation says why. Reason shows as `about:<label>`.
- **Doubling:** sends a copy of a request to the other track and stores both
  answers. Claude Code only ever sees the answer from the track the rules chose.
  Settings are rate (default 0%, so nothing is doubled until you raise it), an
  optional tag (e.g. `both`: only turns with `#both`), and directions (which
  track shadows each track; default Anthropic and Valar shadow each other, local
  none). It picks whole turns: every
  `POST /v1/messages` of a chosen turn is doubled. Copies cost Max quota (to
  Anthropic), Valar credit (to Valar) and GPU time (to local). At most two
  Anthropic, two Valar and one local copy run at once; a step that finds its direction full is marked "skipped, busy".
  Judge pairs in Side by side; "Forget all pairs" deletes them.

### Rule language

A rule is one or more lines: `when <condition> go anthropic|valar|local`. The first
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
non-Anthropic turns (lever on Valar or Local, `#local`, `#execution`, or a rule or
the classifier sending the main session to Valar or local), sending it back to Anthropic may be rejected.
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

ValarCode (Valar's own agent product) was evaluated and rejected: this router
only uses Valar's Anthropic-compatible API.

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

## Main session on Valar or local (accepted limitation)

`#local`, `#execution` or `/model sonnet` works in the main session. The catch is that later
requests to Anthropic then carry assistant turns the Valar or local model generated,
such as thinking blocks without valid Anthropic signatures, and Anthropic may
reject them, which breaks that conversation. The router does no history
cleaning. **Keep the main session on opus. Use `#local` or `#execution` in the main session
only for throwaway conversations.** Tags are mainly for steering subagents. A
main session started on sonnet runs entirely on Valar (default rulebook).

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

### Results: Valar track (2026-10-28, Claude Code, real Valar, LiteLLM not running)

Router on 127.0.0.1:8799 with a scratch config dir; keys not logged.

1. `claude -p "hi"`: pass.
   ```
   route=anthropic reason="rule:Model the request asked for:1" model=claude-opus-5-5 status=200 ms=1728 stream=True
   ```
2. `claude -p "#execution say hi"`: pass (200), but the first Valar attempt
   returned 400 and Claude Code's retry got 200.
   ```
   route=valar reason="rule:Tags in your message:3" model=claude-opus-5-5→zai-org/GLM-5.3 status=400 ms=988 stream=True
   route=valar reason="rule:Tags in your message:3" model=claude-opus-5-5→zai-org/GLM-5.3 status=200 ms=3086 stream=True
   ```
3. Haiku subagent: pass (routed by the classifier, `about:chit-chat`, within
   the time limit); same 400-then-200 pattern.
   ```
   route=anthropic reason="rule:Model the request asked for:1" model=claude-opus-5-5 status=200 ms=3693 stream=True
   route=valar reason=about:chit-chat model=claude-haiku-5-5→deepseek-ai/DeepSeek-V4.1-Flash-1017 status=400 ms=2835 stream=True
   route=valar reason=about:chit-chat model=claude-haiku-5-5→deepseek-ai/DeepSeek-V4.1-Flash-1017 status=200 ms=3680 stream=True
   ```
4. `claude -p "#local hi"`: pass. LiteLLM unreachable, 502 on every attempt
   (Claude Code retried), no other route (anthropic lines were only
   `passthrough` housekeeping).
   ```
   route=local reason="rule:Tags in your message:2" model=claude-opus-5-5→claude-sonnet-4-6 status=502 ms=2 stream=True
   ```
5. Sonnet subagent with the classifier on: pass, `about:chit-chat`.
   ```
   route=valar reason=about:chit-chat model=claude-sonnet-5-5→zai-org/GLM-5.3 status=200 ms=1958 stream=True
   ```
