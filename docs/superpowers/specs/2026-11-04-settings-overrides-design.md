# Settings overrides: per session and on a schedule

## Why

How Valar handles a request is a trade between speed and price.

- **Speed vs price:** the completion window `asap` ("now") answers fast; `priority` is cheaper.
- **The right choice varies:**
  - A session you are driving should answer fast.
  - A background session nobody watches can wait.
  - Overnight (for example 00:00–07:00) nothing is waiting, so everything can use the cheaper window.
- **What exists today:** the window and the model name are set once, per downstream and model tier. They apply to
  every session at every hour.

The user wants to override these settings for a chosen set of sessions, or for a time of day, or both. They want to
do it from the console and from inside a Claude Code session. Example: everything uses `asap`, and picked sessions or
00:00–07:00 use `priority`.

## Goals

- **The override list.** A new ordered list of *settings lines*. A line says when it applies, then which setting it
  changes on which downstream.
- **What a line can change, for one downstream:**
  - **window:** Valar-kind downstreams only. The values are `routing.WINDOWS` (`asap`, `priority`, `standard`).
  - **model:** any downstream. This is the model name the router sends for a tier, in place of the one in
    `Downstream.models`.
- **Which tiers a line covers:** every tier, or one named tier (`fable`, `opus`, `sonnet`, `haiku`).
- **Conditions:** the existing rule language, plus two new fields:
  - `session`: the Claude Code session id, a word, compared with `is`.
  - `hour`: the router's local hour, 0–23, a number. It works with the existing `>= < > <=` operators.
  - A range that wraps past midnight is written with `or`: `hour >= 22 or hour < 6`. No new range syntax.
- **Order:** the list runs top to bottom. For each (downstream, tier, setting), the first line that matches wins.
  Lines that change different settings both apply.
- **Collision warning.**
  - Two lines collide when all three hold:
    - they change the same setting, on the same downstream, for overlapping tiers;
    - they set different values;
    - their conditions are not provably disjoint.
  - The console marks both lines and says which one wins, for example "line 3 is shadowed by line 1 from 00:00 to
    07:00". Saving still works, since a deliberate override is a collision on purpose.
- **Picking sessions in the console:**
  - Pick sessions with the existing session chips, then add a settings line.
  - The new line comes pre-filled with `session is A or session is B`.
  - Session ids show by their chip label wherever the console shows a line.
- **Setting it from inside a session.** The plugin's `/router` grows a settings form:
  - `/router <downstream> [<tier>] window <value>`
  - `/router <downstream> [<tier>] model <name>`
  - `/router <downstream> reset`: removes this session's lines for that downstream.

  The command adds or updates a line for this session alone, at the top of the list, so it beats schedules.
- **Session lines are saved.** They survive a router restart.
  - A *session line* is one whose condition is only `session is …` terms joined by `or`.
  - **Daily pruning:** session ids with no request in 7 days are dropped from these lines, by `sessions.last_ts`.
  - A line left with no sessions is removed.
  - Lines with any other condition are never pruned.
- **Visibility.** A request that a line changed shows it on its departure row next to the reason, for example
  `window: priority`, with the matching line's condition in the tooltip. It is stored with the request (setting,
  value and the condition text, not a line number, since lines get renumbered), so it survives a reload and edits.

## Non-goals

- **No track choice.** Settings lines never decide where a request goes. Routing rules and the lever do that,
  unchanged.
- **No Anthropic settings.** Anthropic has none to override.
- **No price, URL or key overrides.**
- **No minute-level schedules or day-of-week fields.** Hours are enough for now. Mark the field with a `ponytail:`
  comment that names this.
- **No backfill.** Old rows have no override text.

## Anti-goals

- **Never fail closed.** A broken settings list must not cost Claude Code its router. Use the existing pattern:
  fallback plus banner.
  - An invalid list loads as empty, with a banner.
  - A line that refers to a deleted downstream is skipped.
- **An untouched Anthropic request still goes out byte for byte.**
- **No hidden schedules.** The console shows the router's current time and time zone next to the editor, so a wrong
  zone is visible.
- **No switching on Claude's judgment.** The settings form of `/router` keeps the plugin's gate: one switch per
  `/router …` the user typed.

## Constraints and how it fits the existing code

- **Lever independence.** Settings lines apply whatever the lever says, global or per session. Only the track choice
  skips rules. This is why they are a separate list, not routing rules.
- **The single prep step.** Apply overrides in `prepare()` in `signaller.py`, which today calls `track_model` and
  `track_window`. This is the one place that renames models and sets windows, for served and shadow requests alike.
  - The shadow follows the lines for its own track, with the same session and hour.
  - `prepare()` needs the session id and the time. Pass them in; don't re-derive them.
- **Prices.** A request is priced at the window and model it was actually sent with. This already follows from
  `prepare()`'s returned `out_model` and `window`.
- **New fields are available to routing rules too.** `session` and `hour` are added to `rulelang.FIELDS` and
  `Features`, so routing rules can use them. That is a free side effect, not a goal.
- **Config.** `console.json` goes to version 4:
  - It holds the settings list.
  - It gets a migration from v3 (empty list) in `routing.config_from_json`, and a v3 fixture under
    `tests/fixtures/`.
  - `RESERVED_IDS` must keep `session` and `hour` from becoming downstream ids, as it does for the other fields.
- **Deleting a downstream.** `routing.references` must list the settings lines that name a downstream, so they block
  its deletion like other references do.
- **Validation on save:**
  - The downstream exists.
  - A window line names a Valar-kind downstream.
  - The window value is in `WINDOWS`.
  - The tier is in `MODEL_TIERS`.
  - The model is a non-empty name.
  - The condition parses.
  - An error points at line and column, as rule errors do today.
- **Storage.** The `requests` table gets a nullable TEXT column for the override description. Add it in three places:
  `SCHEMA`, `COLS` and the `_migrate` types map.
- **Time zone.** Docker runs the container on UTC. The compose setup must give the router the host's local zone, for
  example via `TZ` or a mounted `/etc/localtime`. `hour` reads local time.
- **Plugin.**
  - The command sets the line through a new console API endpoint, with the same loopback Origin check as other
    writes.
  - Bump the version in `plugin/signaller/.claude-plugin/plugin.json`.
  - The `router_lever` tool, if it gains the settings form, keeps the typed-command gate.

## Tests

One test per piece of logic, per AGENTS.md:

- **Rule language:** `session` and `hour` parse and evaluate. A wrapped hour range matches at 23 and at 3, not at 12.
- **Override resolution:** first match wins per (downstream, tier, setting). Two lines changing different settings
  both apply. With no matching line, the result equals today's.
- **Collision detection:**
  - Overlapping lines are flagged.
  - Disjoint session lists are not flagged.
  - Non-overlapping pure hour ranges are not flagged.
- **Config:** a v3 file loads as v4 with an empty list. A window line on a non-Valar downstream is rejected.
- **Proxy:**
  - A matching line changes the window header and the model sent upstream.
  - The row records the override.
  - The shadow uses its own track's lines.
- **Pruning:** a session not seen in 7 days is dropped from a session line, and an emptied line is removed.
- **Console API:** the session endpoint adds a top line, updates it on a second call, and `reset` removes it.
- **Plugin:** the gate test covers the settings form.
- **UI:** checked in a browser against a scratch router, as AGENTS.md says.

## Calls made while writing this (not discussed; check them)

- **Gate:** a typed `/router <downstream> … <setting> <value>` grants exactly one write of exactly that command, with
  those values. The grant parser in `register.ts` must capture the whole command, not only the first word.
- **A session line's identity:** (session, downstream, tier or "every tier", setting). A second command with the same
  key replaces the value. A different tier or setting adds another line at the top.
- **Leaving out the tier:** the line covers every tier. For `model`, that sends all four tiers as one model name.
- **The classifier's own request is exempt.** It has its own model setting and no session, and lines don't touch it.
- **Order stays user-controlled.** The console can move any line. `/router` and the session picker only *insert* at
  the top, so a session line beats schedules until you move it.

## Left to the builder

- Exact line syntax. Something like `when hour >= 0 and hour < 7 set valar window priority` or
  `when session is X set valar opus model glm-5.4`, as long as it reads like the existing rules.
- Whether the list is stored as one text block (like a rule) or as structured entries.
- How "provably disjoint" is checked. It must handle pure session lists and pure hour comparisons. Anything harder may
  be treated as overlapping.
- Where the editor sits in the console (a panel next to the rules is the obvious spot), and its wording.
- When pruning runs: at startup and once a day, or on the store's writer thread.
