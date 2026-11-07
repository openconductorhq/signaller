# Auto mode — design

## Why

Routing today is static: the rulebook decides from side signals (subagent → Valar,
teammate → Valar, model tier on the main thread). To get the right track the person
has to think about how they prompt, tag, or flip `/signaller` levers. They want to
code without managing routing.

The person's split is simple and theirs:

- **Planning** — brainstorming, writing or refining a spec, writing or refining an
  implementation plan. Goes to **Anthropic**.
- **Execution** — everything else: implementing, fixing, reviewing (code, specs,
  plans), verification, research, ops, status. Goes to the **execution downstream**
  (Valar today).

Measured on 5,149 city-os prompts (Claude-labelled, prompt by prompt, out of context):
97% of main-thread turns are execution yet go to Anthropic; ~1.4% of subagent and ~3.2%
of teammate briefs are spec/plan writing yet go to Valar. That per-prompt labelling
under-counts planning, because a reply inside a brainstorm ("2", "yes", "no, I also use
rules…") is planning by its phase, not its text — the main lesson behind this design.

## Goals

- A new lever mode, **`auto`**, next to `rules`, `anthropic` and the downstreams:
  set per session (`/signaller auto`, the plugin tool under the same typed-step gate)
  or as the global lever in the console.
- In auto, each request goes to Anthropic if the conversation is in a planning phase,
  else to the execution downstream.
- **Fast and local.** No LLM call per prompt, nothing sent off the machine to decide.
  The decision adds well under 10 ms to a request.
- Measured before it ships: an offline evaluation on real sessions shows auto beats
  today's rules on the person's definition.

## Non-goals

- No hosted decision model (Jev and similar were considered and declined: the prompt
  text would leave the machine; no self-hostable version exists).
- No change to how `rules` mode works. Auto is opt-in.
- No learning online from traffic. Retraining is a manual, offline step.

## Anti-goals

- A slow or broken auto must never cost a request: fall back, never fail closed.
- Never route a planning phase to the cheap track because of one terse reply.
- No prompt text, training data or trained weights in the repo (the repo is shared;
  weights trained on prompts can leak their words).

## How auto decides

In order, first that decides wins:

1. **Always-on overrides** (same as every mode): requests with a server-side tool only
   Anthropic runs → Anthropic; the image fallback still retries on Anthropic.
2. **Tags** at the very start of the prompt: `#planning` → Anthropic, `#execution` →
   execution downstream.
3. **Phase**, read from the conversation history the request already carries:
   - **Planning starts** when a planning skill starts: a brainstorming, spec-writing or
     plan-writing skill (in Claude Code, a `Skill` tool call or a typed slash command
     naming it).
   - **Planning ends** when implementation starts, whichever comes first: a skill that
     executes a plan or dispatches implementers starts, or the first file edit
     (`Edit`/`Write`/`NotebookEdit`; not `MultiEdit` or Bash) outside the spec and plan folders.
   - Every request in between is planning, whatever the message says.
   - **One way only:** a session moves from planning to execution, never back. Once a
     session has gone to execution, auto keeps it there, even if a planning skill
     starts later (the person's call: flows rarely switch back, and switching lanes
     loses the prompt cache and mixes histories). A `#planning` tag still sends that
     one turn to Anthropic.
   - The phase comes from the request's own history, with no saved per-session state
     (a rewound or forked conversation must route by what it actually holds). A
     message the content model calls planning opens a phase just as a planning skill
     does, and it lasts until an end event. After a compaction drops the history that
     opened the phase, `#planning` restores it.
   - The skill lists and the spec/plan folders are configurable in the console, with
     defaults for the superpowers skills and `docs/superpowers/specs|plans`.
4. **Content model**, for what the phase can't see (planning started in plain words,
   "let's spec this out"; a subagent or teammate brief that writes a spec or plan).
   A small trained classifier over the newest human text (the brief, for a subagent or
   teammate) answers planning/execution with a confidence; it decides only above a
   threshold.
5. **Default: execution.**

Subagents and teammates use the same steps over their own history; their brief is
their newest human text.

**Tracks change only between turns.** The decision is made at a turn's first request
and held for the whole turn, so a tool loop never changes track in the middle; a phase
change seen mid-turn takes effect at the next human turn. A turn starts at a user
message with human text and no tool result: text Claude Code adds to a tool-result
message (a skill's body, a task notification, an interrupt's new prompt) continues
the turn.

**Mixed history.** On a request going to Anthropic, auto drops a downstream's
thinking blocks from earlier turns, so they can't break the conversation. A
downstream's block is told by its signature: the downstreams seen send an empty one
or a 6-character stub, Anthropic's are 452+ characters. Anthropic's own signed
thinking stays (current models keep earlier thinking in context, so dropping it
would cost cache reads), as do redacted thinking, the current turn, and a message
the drop would leave empty. This is the one exception to "an untouched Anthropic
request goes out byte for byte", and it applies only in auto mode, only when there is
a block to drop. With the one-way rule it rarely matters, but tags and subagent briefs
can still reach Anthropic after a downstream turn.

**Phase only works without the model.** The content model is optional: with no
weights file, auto runs steps 1–3 and 5, and can be used before any model is trained.

Schedule (settings) lines apply in auto as in every mode: they run after the track is
chosen.

The reason shown in the console and the explanation reads like
`auto:planning (phase: brainstorming)` or `auto:execution (model 0.93)`. The plugin's
status line shows `auto · planning` / `auto · execution`.

Which downstream "execution" means is a console setting, defaulting to the first
downstream (Valar).

## The content model

- **Data:** the person's own Claude Code transcripts (city-os first; other projects
  later). Labels by Claude under the definition above, **phase-aware**: each request is
  labelled with its conversation context, not alone. The person spot-checks a sample of
  ~50.
- **Training and evaluation** happen offline in a scratch environment (any libraries
  allowed there). The router itself stays standard-library only.
- **Shape (builder's choice, recommendation):** start with a linear model over word and
  character n-grams (TF-IDF + logistic regression) exported as a JSON weights file the
  router scores in pure Python in microseconds. Move to a small embedding or fine-tuned
  encoder model served locally (the Mac, or a DGX Spark) behind an HTTP call with a
  short timeout only if the evaluation shows the linear model falls short.
- **Where it lives:** the weights file goes in the config dir (`~/.config/signaller`),
  never the repo. No file → step 4 is skipped, with a banner in the console.

## How we know it works

An offline evaluation replays the labelled sessions request by request and scores
three deciders on held-out sessions (split by session, not by prompt):
today's rules; phase alone (steps 1–3, 5); phase plus content model (all steps).

Reported: accuracy; misroutes each way (planning → execution costs quality, execution
→ Anthropic costs money); estimated cost moved off Anthropic, net of the prompt cache
lost at each switch; decision time per request. Auto ships when phase plus model beats today's rules on the person's
definition and planning → execution misroutes are rare (target to be set from the
first evaluation, with the person).

## Constraints

- Router: Python 3 standard library only, no build step (AGENTS.md).
- Never fail closed: a missing or unreadable weights file, a scan error or a slow
  model call falls back to the next step, with a banner.
- No request bodies, prompts or labels in logs.
- The `signaller_lever` gate stays: `/signaller auto` must be typed for Claude to
  switch to it.
- Test on a scratch router (8788), never the live one.

## Left to the builder

- Exact detection of skill starts and file edits in a request's history, and the
  default skill lists.
- Model type and features beyond the recommendation, the confidence threshold (from
  the evaluation), and the dataset tooling.
- Console layout for the auto settings.
