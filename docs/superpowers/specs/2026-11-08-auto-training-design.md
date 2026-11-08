# Auto training: each person teaches auto from their own sessions

Follow-up to auto mode (PR #10). Status: design, not approved yet.

## Why

Auto mode's optional model (`<config>/auto_model.json`) was trained by hand,
once, on one person's city-os sessions with Claude's labels. Nobody else can
make one, and the one that exists is fitted to that person. The person wants
signaller itself to offer the training, so that anyone who turns on auto can
fit it to how *they* work, with no hand-tuning by anyone.

Success: a person on auto with no model is led, pleasantly and without
nagging, to train one; picks projects; sees what it costs before anything
runs; and ends at a result card that shows, in their own numbers, whether
their model beats rules alone, with one click to use it.

## Goals

- Training is a signaller feature: pick projects → label → train → evaluate →
  result → use it.
- The person picks **projects** (Claude Code's `~/.claude/projects/<dir>`), not
  sessions. Signaller takes every session in them.
- Labels come from Claude, run **through the plugin in Claude Code**, on the
  person's own Claude Code usage. The router holds no API key for this.
- When labelling would be costly, signaller offers to cut down to the newest
  sessions under a threshold, before anything is sent.
- The UX is the person's chosen option "C": two entry points (a band in Claude
  Code and a "Teach auto how you work" card in the console's Auto box), one
  command (`/signaller train <projects>`), and a result card as the payoff.

## Non-goals

- A shipped, pre-trained model for everyone.
- A bigger model (fine-tuned LLM, embeddings). Same `signaller-auto-1` format:
  binary unigram+bigram features, logistic, scored in stdlib.
- Retraining on a schedule. A person can run the command again.
- Changing how auto decides (phase rules first, model as the extra planning
  opener, threshold). This feature only produces and installs the model.

## Anti-goals (a failure even if it works)

- A band that nags: shown every turn forever, or in sessions not on auto.
- Prompt text leaving the machine other than to Claude for labelling, or
  landing in logs, `console.json`, the repo, API responses or the model file
  (the model holds word weights only).
- A result card that oversells: a model that doesn't beat rules alone must not
  be the highlighted choice.
- Anything that breaks the router: training failures fall back with a
  message, never a dead router (AGENTS.md "never fail closed").
- Labelling that writes "/signaller" into subagent or skill prompts (the lever
  gate reads that text; see AGENTS.md Plugin).

## What the person sees

1. **Band (Claude Code)** — while the session's lever is auto and no model is
   in use: a line above the prompt saying auto is running on rules only and
   `/signaller train` teaches it this person's habits, with a rough cost.
   *My call:* shown in at most the first 3 sessions on auto, then only the
   console card remains; never in sessions on another lever; gone once a model
   is in use or the person dismisses it.
2. **Console card (Auto box)** — "Teach auto how you work": projects that have
   sessions, each with a session count and a checkbox; the prompt count and an
   estimated label cost and time for the ticked set; the cut-down offer when
   over the threshold; a ready `/signaller train …` command with Copy.
3. **The command** — `/signaller train` with no projects asks which, inline
   (same list and estimate as the card). With projects, it confirms count and
   cost (and offers the cut) before labelling starts. Progress shows in the
   transcript and in the console card.
4. **Result card** — in the console and as a transcript line, from sessions
   held out of training: planning turns kept on Anthropic and execution turns
   moved to the downstream, for *their model* against *rules alone*. Buttons:
   **Use it** (installs the model, no restart) and **Keep rules only**. If the
   model doesn't beat rules alone, say so plainly and make Keep the default.

## Decided

- Labelling via the plugin in Claude Code (the person's choice), not an API
  call from the router.
- Project-level selection, all sessions, with a cost cut-off offer.
- UX option C.
- **Ask once:** `/signaller train` confirms count and cost one time, then labels
  every batch unattended in that Claude Code session (as the hand-made run
  did). No per-batch approval.
- Definition used for labels (the person's words): anything that isn't
  brainstorming, speccing or planning is execution.

## My calls (marked so the person can check them)

- **Split of work:** the router reads transcripts, builds the prompt list,
  estimates cost, trains, evaluates and installs; the plugin only drives
  labelling and shows UI. Training is stdlib (logistic regression by gradient
  descent over the existing `autophase.features`); `tools/auto_train.py`'s
  scikit-learn path stays as an offline tool. Seconds on thousands of prompts.
- **Which prompts:** main-thread human prompts only (the same rule as
  `autophase.turn_index`), as in the hand-made dataset. Subagent and teammate
  requests are execution by construction and need no labels.
- **Cut-off:** newest sessions first. Threshold default is a dollar figure
  shown in the card and editable there; *my call* $5.
- **Evaluation:** hold out 20% of sessions by session id (never by prompt),
  replay through rules-only and phase+model, report the two numbers above.
  Fewer than ~30 labelled planning prompts → say there isn't enough to judge.
- **Use it without restart:** the router reloads `auto_model.json` on install
  (today it is read at start only).
- **Where data lives:** labels and the prompt list in the config dir under a
  git-ignored, 0600 `training/` folder, deleted after a successful install
  unless the person keeps them; never served raw by the API.
- **No prompt text crosses the API.** The router serves the plugin only
  session ids and turn indexes to label; the labelling side reads the text
  straight from the transcripts, and labels come back as
  `(session, turn, label)`.
- **Label only what the model can change:** skip prompts the rules already
  settle (lead tags, typed skill commands, short follow-ons, as
  `auto_train.train_rows` drops them). Cuts cost and count.
- **Labelling model:** a fixed cheap one (Haiku), since the person judged
  labelling an easy task; the estimate uses its rate, so the band's cost is
  honest. Spot-checking label quality on a sample is part of the eval.
- **Training never runs on a request thread:** a worker thread, with progress
  served to the card. "Seconds" is to be measured during the build; if a
  stdlib fit is slow, cap features or rows rather than block.
- **Transcript access:** Docker already mounts `~/.claude/projects` read-only
  at `/claude/projects`, today for session labels only. Reading prompt text
  from it widens what that mount is used for; the README's privacy section
  must say so.

## Left to the builder

- How the plugin runs labelling unattended after the one confirmation: the
  plugin's own model calls, or plugin-driven subagents. Constraints: runs on the
  person's Claude Code, batches prompts, uses the fixed definition above, and
  never puts "/signaller" in a subagent or skill prompt.
- How labels get from the plugin to the router (a console API endpoint under
  `/ui/api/`, loopback Origin like the rest).
- Cost estimate method (tokens per prompt × the current model's rate; show it
  as an estimate).
- Band and card copy, within the tone above.
- Tests per AGENTS.md: one per new logic path, an API test for each endpoint,
  a gate test for the command, a browser check of the card on a scratch router.
