# Presets: save, switch, share a whole Signaller setup

## Why

A developer tunes Signaller (rulebook, downstreams, models per tier, schedules,
saved prompts) and wants a colleague to try that setup "for an afternoon", then
go back to their own. Today a setup lives only in one `console.json`, and there
is no way to keep two side by side, switch between them, or hand one over.

Success: a colleague sends a file, you see what it would do before anything
changes, add it in one click, enter any key you're missing, switch to it, and
switch back later to find your own setup exactly as you left it.

## Goals

- Save the current setup as a named preset, with a tagline (what it's good for).
- Keep several presets and switch between them quickly, from the console only.
- Export a preset as a file; import one with a preview before it's added.
- Keys never leave the machine: never in an export, never shown in a preview.

## Non-goals

- Switching presets from Claude Code (`/signaller`). Console only (the person's
  decision): a switch changes routing for every session on the machine.
- Merging two presets, or importing only part of one.
- Syncing presets between machines, or any hosted sharing. A file is the unit.
- Per-session presets. A preset is global, like the global lever.

## Anti-goals (a failure even if it "works")

- A key sent to a server it wasn't entered for. If an imported preset names a
  downstream id you already have a key for, but at a different host, your key
  must not go there.
- Anything changing on import before the person clicks "Add".
- An import that silently breaks routing: a preset that needs a key you don't
  have must say so, loudly, until the key is entered.
- Losing the person's own setup: switching away and back must restore it
  exactly, including edits made since it was last active.

## What a preset holds

Everything that defines routing, which is what `console.json` holds today:

- the rulebook, the otherwise track, the global lever
- the classifier settings and labels, doubling
- the downstreams: id, name, URL, kind, colour, models per tier, windows, prices
- settings lines (window/model overrides)
- saved prompts (`/signaller run <name>`); they belong to the preset (the
  person's decision)

Plus its own **name** and **tagline**.

Never in a preset: downstream keys, stats, per-session levers.

**Session-bound settings lines** (`session is …`) stay with the preset they
were written in, but are left out of an export: they name the exporter's own
Claude Code sessions, which mean nothing on another machine.

## Behaviour

### The active preset

- Exactly one preset is active. Every edit in the console, and every
  `/signaller` settings write or prompt, changes the active preset (the
  person's decision: "the active preset is the one you edit").
- The first time the new version runs, the existing setup becomes a preset
  named "my setup" (my call), active, so nothing changes for the user.
- The active preset's name is always visible at the head of the page, next to
  the "signaller" title (the person's decision, for transparency).

### Presets section (console)

A **Presets** section of cards, above Tracks (the person picked this layout,
option B of the mockup). One card per preset:

- name, tagline, a one-line summary (e.g. "5 rules · Valar · opus→Anthropic,
  rest→GLM · night: priority")
- the active one is highlighted and says "running"
- on others: **Switch**, **Export**, **Delete**; on the active one: **Export**
- a card whose downstreams lack keys says so in amber ("Lemmi needs a key")
- a last card: **+ Save current as…** (name + tagline) and **Import…**

Deleting the active preset isn't possible (switch first). Names are unique.

### Switching

One click on Switch. The current setup is kept as it stands in its own preset,
and the target becomes active. Routing changes from the next request. If the
target lacks keys, the switch still happens (my call: the person may want to
fill them in on that preset's Tracks) and the missing-key banner shows.

### Export

From a card: the person can set the exported name and tagline (the person's
decision), then gets a `.json` file download. The file carries a format
version, the name, tagline, export date and the preset's content, minus keys
and session-bound settings lines.

### Import

The person picks or drops a `.json` file (my call: also paste text into a box,
for sharing over chat without a file). A **preview** shows before anything is
added (the person approved this mockup, see `import-preview` below):

- header: name, tagline, export date
- **Tracks:** each downstream as a coloured bar, with its URL host and models
  per tier and window. Marked green "your key works here" when you have a key
  for that downstream id **at the same host**; amber "new · needs a key"
  otherwise.
- **How a request is routed:** the rules in order, in plain words, with track
  names in their colours; then classifier, otherwise, doubling in one line.
- **Schedule:** settings lines, as "22–6: Valar window Priority".
- **Prompts:** the saved prompt names.
- the person can change the name and tagline in the preview before saving
  (the person's decision: "override and then save")
- **Add to my presets** / Cancel. Adding never switches (the person's
  decision). If keys are missing, the line beside the button says which to
  enter, and where.

An invalid file is refused with a plain reason ("this isn't a Signaller
preset", "made by a newer Signaller", the first validation error). A name that
already exists asks to rename in the preview.

### Missing keys

After an import or switch, any downstream in the active preset without a
usable key shows a banner at the top of the page ("Lemmi needs a key: enter it
in Tracks") until the key is entered. Requests routed to a downstream without a
key already answer 502 naming the downstream; that stays.

### Keys and hosts (security)

Keys stay outside presets, in the one key store, but keyed by **downstream id
and URL host** (my call). A key is only ever sent to the host it was entered
for. So a colleague's `lemmi` at another host gets its own key slot: entering
it never overwrites the key your own `lemmi` uses, and switching back finds
yours untouched. On upgrade, every existing key counts as entered for the host
its downstream has in the current setup, so nothing breaks. Changing a
downstream's URL to a new host means entering its key again there.

### Machine state is not preset content

`seeded` (the Valar URL/key taken from the environment once) and
`migrated_off` (a banner note from an old migration) describe this machine,
not a setup. They are not exported, and switching or importing never triggers
seeding from the environment (my call).

## Folded into this work

1. **Per-session levers survive a restart.** Today `App.session_levers` is
   memory only, so a container rebuild drops `/signaller anthropic` and the
   session falls back to the global lever. Persist them in the config dir,
   load at start, drop ids unseen for a week (the rule settings lines use).
   They are not part of any preset. A session lever naming a track the active
   preset doesn't have is ignored while that preset is active (the session
   follows the preset's lever, and the status line shows that), but kept, so
   switching back restores it (my call). One test.
2. **The image-fallback warning moves bottom-left.** A plugin can't move a
   toast (Claude Code stacks them top-right). Replace the toast with an amber
   band above the prompt box (`ui.render` on `AbovePrompt`, which this build's
   plugin API has; the person's other plugins already draw there), shown until the
   next prompt; keep the transcript line. Plugin version bump, plugin test.

## Constraints

- Python 3 standard library only; console is vanilla JS/CSS under the strict
  CSP (no inline styles/scripts, colours via `style.setProperty`), per
  AGENTS.md.
- `console.json` version 4 must keep loading; a shape change gets a migration
  in `routing.config_from_json` and a fixture.
- The config API's `rev`/409 protection must cover preset switches and imports
  too: a page holding an older copy must not overwrite them.
- Never fail closed: an unreadable presets store falls back to the current
  setup with a banner, never to a dead router.
- Tests stay green; one new test per new piece of logic (store/API/plugin).
- UI checked in a browser against a scratch router, never the live one.
- README and AGENTS.md updated with the feature.

## Left to the builder

- Where presets are stored (one file, a folder of files, inside
  `console.json`) and how the active one relates to `console.json`.
- The export file's exact shape and extension.
- API endpoints and their names.
- The summary line's wording, card styling, empty states, and the exact
  plain-words rendering of rules (it may reuse the console's existing rule
  rendering and colours).
- How the session-lever file is shaped and pruned.

## Calls I made (for the person to check)

- First run: the existing setup becomes "my setup", active.
- Import also accepts pasted text, not only a file.
- Switching to a preset with missing keys is allowed, with a banner.
- Keys are stored per downstream id and host; a key only goes to the host it
  was entered for, and existing keys migrate to their current host.
- A session lever naming a track the active preset lacks is ignored, not
  deleted, while that preset is active.
- `seeded` and `migrated_off` are machine state, never preset content.
- Session-bound settings lines are left out of exports.

## References

- Mockups: `.superpowers/brainstorm/6761-1793885823/content/`
  (`presets-switcher.html`, option B; `import-preview-v2.html`)
