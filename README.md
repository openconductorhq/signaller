# claude-router

A loopback HTTP proxy that sits at Claude Code's `ANTHROPIC_BASE_URL`. It sends
each request to Anthropic (untouched, using your own login) or to a downstream
you added in the console (credentials swapped, model renamed). Valar is the
first one. Python 3 standard library only, in a single file.

```
claude ──ANTHROPIC_BASE_URL──▶ claude-router (127.0.0.1:8787) ──┬─▶ https://api.anthropic.com  (your auth, untouched)
                                                                ├─▶ https://api.valarhq.ai      (Valar key, model renamed)
                                                                └─▶ any downstream you add      (its key, model renamed)
```

## Routing

Applies to `POST /v1/messages` and `/v1/messages/count_tokens`. Everything
else, including any body that can't be parsed, goes to Anthropic unchanged.
The console's rulebook decides; a fresh config ships this default (first
match wins):

| Signal | Track |
|--------|-------|
| tag `#planning` | Anthropic |
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

On a downstream's route the model is renamed from its own model map. For
Valar, `sonnet` becomes `zai-org/GLM-5.3` and `haiku` becomes
`deepseek-ai/DeepSeek-V4.1-Flash-1017`. The maps are edited on the console page
(Tracks panel), not by env vars. Your
`Authorization`/`x-api-key` and the `oauth-2025-04-20` beta are removed, and the
downstream's key is added. A downstream with no key, or that is down, answers
502 and never falls back to another track.

Each request is logged as one line on stderr:
`time route= reason= model=in[→out] status= ms= stream=`. Bodies, prompts and
keys are never logged.

## Install

### In a container (recommended)

```
mkdir -p ~/.config/claude-router
cp env.example ~/.config/claude-router/env
chmod 600 ~/.config/claude-router/env      # then set VALAR_API_KEY if you use Valar (read once, see Downstreams)
docker compose up -d --build
```

`compose.yaml` publishes the router on `127.0.0.1:8787` only, keeps config and
stats in `~/.config/claude-router`, reads `~/.claude/projects` read-only for
session labels. Inside the
container the router listens on `0.0.0.0`, which it allows only with
`CLAUDE_ROUTER_IN_CONTAINER=1` (set by the image). Logs: `docker compose logs -f`.
The image runs as uid 1000; on Linux, if your uid differs, add
`user: "<uid>:<gid>"` to the service.

### As a systemd user service

```
mkdir -p ~/.config/claude-router
cp env.example ~/.config/claude-router/env
chmod 600 ~/.config/claude-router/env      # then set VALAR_API_KEY if you use Valar (read once, see Downstreams)
mkdir -p ~/.config/systemd/user
ln -s $PWD/claude-router.service ~/.config/systemd/user/
systemctl --user enable --now claude-router
```

The unit runs Python with `-E -s` (ignores `PYTHON*` env vars and the user site
directory), so only the standard library is ever loaded.

The router won't start if `CLAUDE_ROUTER_LISTEN` isn't a loopback address; it
exits with code 2. `VALAR_URL` defaults to `https://api.valarhq.ai` (a trailing
`/` or `/v1` is stripped). `VALAR_URL` and `VALAR_API_KEY` are optional and only
seed the Valar downstream once (see [Downstreams](#downstreams)). `LITELLM_*`,
`CLAUDE_ROUTER_LOCAL_SONNET` and `CLAUDE_ROUTER_LOCAL_HAIKU` are no longer
used: add LiteLLM as a downstream in the console, and model names live there.

To turn it on in Claude Code, add this to `~/.claude/settings.json`:

```json
"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}
```

- **Bypass:** `ANTHROPIC_BASE_URL= claude`
- **Router down:** every Claude Code session fails to connect. Use the bypass
  above, or restart the router.
- **Logs:** `journalctl --user -u claude-router -f`

### `/router` in Claude Code

The `router-lever` plugin in `plugin/` adds `/router` (show the mode),
`/router rules|anthropic|<downstream id>` (switch it for this session only; its
subagents follow), `/router reset` (back to the global lever) and
`/router name <text>` (what the console calls this session; `/router name` alone
clears it; kept in `stats.db`), answered
without a model turn, and puts `router: <mode> (this session|global)` in the
status line (checked every 15 s). Other sessions keep the console's lever. A
session's own lever lives in the router's memory, so a router restart drops it.
Claude can switch it too, through the plugin's `router_lever` tool, but only
when you typed `/router <mode>` in a message ("do /router anthropic", or a plan
with "/router rules" at a later step). Each one you type allows one switch to
that mode; anything else is refused. It talks to the console
API (`/ui/api/lever`) on `127.0.0.1:8787`. Install:

```
/plugin install router-lever --marketplace wilfoa/claude-router
```

## Downstreams

A downstream is any Anthropic-compatible service the router can send traffic
to. Add one in the console (Tracks panel, "+ Add downstream"). At most 6, and
any can be deleted, including the last one.

The wizard:

1. **Connect:** a name, an id (suggested from the name; it can't change later),
   the URL and the key. "Test connection" lists the service's models. A failed
   test doesn't stop you saving.
2. **Models:** the model for each tier (fable, opus, sonnet, haiku), or one model
   for all four.
3. **Cost:** optional prices per million tokens. Left empty, it shows as unpriced.

**Valar kind:** a service whose model list carries its own prices (or whose host
is `api.valarhq.ai`) is added as Valar kind. It keeps Valar's extras: prices from
its list, the Now/Priority/Standard window pickers, and the Valar request
fixes. Other downstreams are generic.

**Keys** are in `downstreams.keys.json` (mode 0600), never in `console.json` or
the page. Replace a key from the downstream's row; an empty field keeps it.

**Delete** is blocked while anything still uses the downstream: the lever, a
session's lever, a rule, "Otherwise", a classifier label, the classifier
upstream, or a shadow direction. The message lists what; change those first.
Deleting also removes its key and saved model list.

**Env seed:** on the first start of this version, `VALAR_URL` and `VALAR_API_KEY`
fill in the Valar downstream. After that the env is ignored, so a deleted Valar
stays deleted. Old Local/LiteLLM settings are dropped; rules that sent traffic
there are switched off, and the console banner names them.

In history (Departures, cost, side by side), a deleted downstream's past
requests show as "<id>, removed". Adding one back under the same id picks that
history up again.

## Console

Open `http://127.0.0.1:8787/ui` (loopback only). It shows live traffic, edits
the routing rules, manages downstreams, and compares track answers side by side.

- **Lever:** Anthropic, Rules, or one of your downstreams. Anthropic and each
  downstream send everything to that track (reason `lever`). Rules uses the Rulebook. With one
  session picked, the lever shows and sets that session's own lever (the same one
  `/router` sets, updated live), with "back to global" to drop it.
- **Tracks panel:** one row per downstream: state (key set or not), the model
  map, prices, rename, replace key and Delete; "+ Add downstream" opens the wizard
  (see [Downstreams](#downstreams)). The classifier's upstream and model are here
  too. Edits apply to the next request and are saved to `console.json`.
- **Rulebook:** rules are checked in order, first one that decides wins. Nothing
  decided goes to the "Otherwise" track (default Anthropic). Changes apply to
  the next request and are saved to `console.json`.
- **Classifier:** a rule that asks a small model (Valar by default, or any downstream) what the newest human
  message is about (labels, each with a track), once per turn and cached. **Privacy:** by default the
  classifier sends the newest human message's text to Valar
  (`deepseek-ai/DeepSeek-V4.1-Flash`, 2.5 s timeout, reasoning off). Switch its
  upstream to another downstream to keep that text elsewhere, or to "none" to
  switch the classifier off (its rule then decides nothing). It never runs on opus. If it times out or isn't confident, the rule passes and
  the explanation says why. Reason shows as `about:<label>`.
- **Doubling:** sends a copy of a request to the other track and stores both
  answers. Claude Code only ever sees the answer from the track the rules chose.
  Settings are rate (default 0%, so nothing is doubled until you raise it), an
  optional tag (e.g. `both`: only turns with `#both`), and directions (which
  track shadows each track; default Anthropic and Valar shadow each other, and
  a downstream you add shadows nothing until you set it). It picks whole turns: every
  `POST /v1/messages` of a chosen turn is doubled. Copies cost Max quota (to
  Anthropic) or credit on the downstream they go to. At most two copies per
  destination run at once; a step that finds its direction full is marked "skipped, busy".
  Judge pairs in Side by side; "Forget all pairs" deletes them.

### Sessions and cost

Chips at the top of the page filter by Claude Code session; the time control
sets the span (last 15 min, last hour, whole session, or a range).

- **Chips:** "All" means every session. Click chips to pick one or more; click
  "All" to clear the pick. The picked sessions and the span are remembered in
  the browser.
- **Span:** the diagram, the cost cards and the rule counts follow both the
  pick and the span. Departures and Side by side follow only the pick; they stay
  live feeds and ignore the span.
- **Labels:** a session is labeled `folder · branch`, read from Claude Code's
  transcripts at `<claude dir>/projects/*/<session id>.jsonl` (`CLAUDE_CONFIG_DIR`
  is respected). Only `cwd` and `gitBranch` are read. Subagents count toward
  their parent session. Requests with no session id show as "unknown session".
- **Cost:** Anthropic shows what the traffic would cost at API prices, even on
  a flat plan. Valar is priced from its own `/v1/models` prices. Any other
  downstream is priced from the prices you give it in the wizard or Tracks row;
  with none, it counts as $0 and shows as unpriced. The savings card is an estimate (shown with "≈") because other models
  count tokens differently. Side-by-side comparison requests count in cost but
  not in savings.
- **Valar completion window:** (Valar-kind downstreams only) each model slot (Fable, Opus, Sonnet,
  Haiku) can run in Now, Priority or Standard. Priority costs 75% of Now and
  Standard 50%, but they are slow: measured 2026-10-29 on Kimi-K3 with a tiny
  streamed request, Now took about 2 s and Priority and Standard about 11-13 s.
  So Now is the default. Flex isn't offered: it doesn't stream.
- **Old history:** requests recorded before this release have no session or
  cost.

### Rule language

A rule is one or more lines: `when <condition> go anthropic|<downstream id>`. The first
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
when tier is sonnet or tier is haiku go valar
when text contains "migration" and tier is sonnet go anthropic
when tokens > 120k go anthropic
when not subagent and text matches /^(hi|thanks)\b/i go valar
```

Using `label` makes the classifier run for that request (if it's switched on;
otherwise `label` is `none`). The editor previews a rule against your last 200
requests (kept in memory only, so after a restart it covers only newer ones).

### Switching tracks mid-conversation

Same limitation as [Main session on a downstream](#main-session-on-a-downstream-accepted-limitation) below, easier to hit now: once a conversation has had
non-Anthropic turns (lever on a downstream, `#execution`, or a rule or
the classifier sending the main session to one), sending it back to Anthropic may be rejected.
The router does no history cleaning. If it fails, start a new session. A doubled
copy that Anthropic rejects only shows up as an error on its pair.

### Files

In `~/.config/claude-router/` (override with `CLAUDE_ROUTER_CONFIG_DIR`), mode 0600:

- `console.json`: the routing config. If it's unreadable at startup, the router
  uses defaults, keeps the bad file as `console.json.bad` and shows a banner.
- `prices.json`: Anthropic API prices per tier (fable, opus, sonnet, haiku),
  used for the "at API prices" estimates. Written with defaults on first run;
  edit it to match current prices.
- `downstreams.keys.json`: the downstreams' keys, by id (see Downstreams).
- `models.json`: each downstream's model list. Valar's also carries each
  model's prices from `/v1/models` (refreshed at startup and hourly).
- `stats.db`: SQLite stats. It stores each request's latest user message in
  full (as compressed edits of earlier ones from the same session, so a growing
  conversation costs only its new turns) and the doubled answers (text and tool
  calls), locally. Never headers or keys. Requests are pruned after 30 days; pairs and verdicts stay
  until "Forget all pairs".

ValarCode (Valar's own agent product) was evaluated and rejected: this router
only uses Valar's Anthropic-compatible API.

### Console acceptance

1. Lever to a downstream, `claude -p "hi"` goes there (see the Departures feed);
   lever back to Rules.
2. Add the rule `when text contains "zebra" go <downstream id>`. The preview shows
   matches. `claude -p "zebra"` goes to that downstream.
3. With the classifier on against a real downstream, a haiku subagent prompt
   "fix the failing test" shows `about: coding` (or a timeout note) in its
   explanation.
4. Doubling rate 100%, tag `both`: `claude -p "#both use a haiku subagent to say hi"`
   puts a turn in Side by side, and a verdict saves.
5. With reduced motion set in the OS, there are no moving dots.

## Main session on a downstream (accepted limitation)

`#execution` or `/model sonnet` works in the main session. The catch is that later
requests to Anthropic then carry assistant turns the downstream's model generated,
such as thinking blocks without valid Anthropic signatures, and Anthropic may
reject them, which breaks that conversation. The router does no history
cleaning. **Keep the main session on opus. Use `#execution` in the main session
only for throwaway conversations.** Tags are mainly for steering subagents. A
main session started on sonnet runs entirely on Valar (default rulebook).

## Manual acceptance

Start the router with at least one downstream added, then run (a Valar downstream is assumed below; the routes in the results are from the earlier local setup):

1. `claude -p "hi"`: expect `route=anthropic reason=tier`.
2. `claude -p "#execution say hi"`: expect `route=valar`, status 200.
3. `claude -p "use a general-purpose subagent with model haiku to say hi"`:
   expect opus lines on anthropic and haiku lines on valar.
4. Run a real superpowers plan with `subagent-driven-development`. The
   implementers should hit valar while the orchestrator stays on Anthropic.

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

The first-attempt 400s (items 2 and 3) were Valar rejecting Claude Code's
inline-tools `tool_addition` content blocks ("This request contains a content
block of type tool_addition, which the Messages API does not define"); Claude
Code's retry dropped those blocks, and the tools they carried with them. On
the valar route the router now moves those tool definitions into top-level
`tools` and drops the blocks (never on the Anthropic route). Re-run of
`claude -p "#execution say hi"`: a single Valar request, no 400.
```
route=valar reason="rule:Tags in your message:3" model=claude-opus-5-5→zai-org/GLM-5.3 status=200 ms=2681 stream=True
```
