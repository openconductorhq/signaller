# Downstreams you add from the console: design

## Intent

The router sends Claude Code traffic to downstreams. Today there are exactly three, hardcoded throughout:
Anthropic, Valar and Local (LiteLLM). Adding another Anthropic-compatible service means changing code
and env vars.

The goal is to **productize downstreams**: add a new Anthropic-compatible service from the console with
a wizard, then use it everywhere a track is used today, and delete it from the console. The first real
one is lemmi (`https://lemmi.wilfoaware.com`, key in the git-ignored `.env.lemmi`).

**Done means:** lemmi is added through the wizard, traffic can be routed to it (lever, `/router`,
rules), and it can be deleted from the console.

## Decisions (the person's)

- Generic ability to add downstreams, with a wizard. Not a one-off for lemmi.
- **Anthropic stays fixed.** It is not a downstream you can edit or delete.
- **Valar becomes an ordinary downstream**, on the condition that it keeps today's look and behaviour:
  its colour, model pickers from its model list, its per-model completion window (Now / Priority /
  Standard) and its prices from its model list.
- **Local is removed** as a built-in. LiteLLM can be added back through the wizard like anything else.
- Not every service exposes a model list or prices; the design must cope with both missing.

## What a downstream is

A downstream has:

| Field | Notes |
|---|---|
| `id` | short slug used in rules (`go lemmi`), the lever, `/router lemmi`, stored rows. Derived from the name, editable in the wizard, unique, `[a-z][a-z0-9-]{0,23}`, never `anthropic`, `rules`, `reset`, or a rule-language keyword. Fixed after creation (rows and rules refer to it). |
| `name` | display name ("lemmi"). Editable. |
| `url` | base URL; a trailing `/v1` is stripped (as Valar's is today). |
| key | bearer key. Write-only from the browser (see Keys). |
| `models` | one model id per Claude tier: fable, opus, sonnet, haiku. A one-model service maps all four to that id. |
| `color` | assigned from a fixed palette on creation; Valar keeps today's blue. |
| `prices` | optional, manual: USD per 1M tokens for input, output, cache read, cache write, one set for the whole downstream. Blank = unpriced. |
| `kind` | `valar` or `generic` (below). Set by detection, not chosen by the person. |

### Capabilities, found when the connection is tested

| The service... | What the router does |
|---|---|
| answers `GET /v1/models` with a list | tier pickers are dropdowns of those ids; otherwise free-text ids |
| includes Valar-style `pricing[]` in that list | `kind: valar`: prices come from the list (refreshed hourly, as today), per-model window pickers shown, `X-Valar-Completion-Window` sent, inline-tool hoisting and the classifier's `reasoning_effort: none` applied |
| neither | `kind: generic`: plain pass-through with our key and the tier model rename |

Detection of `valar`: the model list carries Valar's `pricing` entries (what `parse_valar` reads), or the
URL host is `api.valarhq.ai`. Every Valar-specific code path keys off `kind == "valar"`, never off the id
`"valar"`.

## Keys

- Stored in the config dir in a separate file (`downstreams.keys.json`, mode 0600), keyed by downstream id.
  Never in `console.json` and never returned by any API: the console only sees "key set" / "no key".
- The wizard and the Tracks row accept a new key (replace); an empty field keeps the current one.
- Env still works as a seed, **once**: on the first start of this version (the v2 → v3 migration, or a
  fresh install), `VALAR_URL`/`VALAR_API_KEY` fill the Valar downstream's url and key. A `seeded` marker
  in `console.json` records that it ran; env is never read again, so a deleted Valar stays deleted.

## The wizard

Opened from a "+ Add downstream" button in the Tracks panel. Steps:

1. **Connect:** name, URL, key. "Test connection" calls `GET {url}/v1/models` (through the router, which
   holds the key) and shows: reachable or not (with the reason), the models found, and whether it looks
   like Valar.
2. **Models:** a model per tier. With a list: dropdowns, pre-filled with the first listed model for every
   tier when there is only one model (lemmi) or left for the person to pick otherwise. Without a list:
   text fields. A "use one model for every tier" shortcut.
3. **Cost (optional):** the four price fields, shown only for `generic` (Valar prices itself). Blank = unpriced.
4. **Save.** The downstream appears everywhere at once (below).

A connection that fails can still be saved (the person may be offline, or the service may not list
models); the row then says the last test failed. The wizard never sends a real message (no surprise
spend); the model list is the only probe.

## Where a downstream shows up

Everything that is hardcoded to `valar`/`local` today is generated from the downstream list:

- **Yard:** one line per downstream, drawn by JS from the list (today's hand-drawn SVG paths become
  generated curves). Anthropic stays the top line. With Valar as the only downstream it looks as today.
  Up to 6 downstreams; the wizard refuses a 7th.
- **Lever:** Anthropic, Rules, then one button per downstream.
- **`/router` plugin:** modes come from the router (the lever API returns the valid modes), not a
  hardcoded list. The `router_lever` tool's enum and the "/router <mode>" grant use the same list.
- **Rules:** `go <id>` for any downstream id. A rule naming an id that doesn't exist fails validation
  with a clear message, as an unknown track does today.
- **Otherwise, classifier labels, side-by-side shadows, classifier upstream:** pick from Anthropic plus
  the downstreams (the classifier upstream from downstreams only, as today).
- **Cost cards:** Anthropic, one per downstream, and the savings card summing all downstreams.
- **Tracks panel:** one row per downstream (URL, key status, tier models, Valar's windows), with edit and delete.
- **Departures / doubled turns / verdicts:** show the downstream's name and colour.

## Delete

- A "Delete" on the Tracks row, with a confirm.
- Blocked while anything still points at it: the global lever, a session lever, a rule line, Otherwise,
  a classifier label, the classifier upstream, or a shadow. The message lists each reference so the
  person can fix them. (No silent re-pointing.)
- Deleting removes the downstream and its key. Stored rows keep the id; the console shows them under the
  id in grey ("lemmi, removed") in history and cost for spans that include them.

## Migration from today's config

On first start with this version (console.json `version: 2` → `3`):

- Valar becomes downstream `valar` (`kind: valar`) with today's models, windows, colour, and the env url/key.
- Local is dropped. Anything that pointed at `local`:
  - lever `local` → `rules`
  - Otherwise `local` → `anthropic`
  - classifier upstream `local` → `valar`; a classifier label's track `local` → `anthropic`
  - shadow to/from `local` → off
  - the built-in "Tags" rule, if its text is still the shipped default: its `when tag is local go local`
    line is removed (so `#planning` and `#execution` keep working; `#local` falls through to later rules).
  - any other rule whose text has `go local` (including an edited Tags rule): the whole rule is switched
    off, text kept, and the console shows a banner naming those rules.
  - The migration must never fail the config load (today a failed load renames `console.json` to `.bad`
    and starts from defaults). See "Validation of switched-off rules".
- `LITELLM_URL` / `LITELLM_API_KEY` and the compose `LITELLM_URL` line are removed; a set `LITELLM_*` env
  var is ignored with a one-line startup notice.
- Stored rows with `track = local` stay and show as "local, removed".

## Calls made after review (the builder's, mine)

- **Fresh install:** ships with the Valar downstream (`kind: valar`, today's defaults), keyless unless the
  env seed provides a key, exactly like today's "no key" state.
- **Zero downstreams is allowed.** Any downstream can be deleted, including the last one. With none,
  the lever is Anthropic / Rules only, the yard shows the Anthropic line alone, and the classifier
  upstream may be `none` (the classifier rule then decides nothing, as if switched off).
- **Validation of switched-off rules:** a switched-off rule is parsed for syntax only; an unknown
  downstream id in it is allowed. Switching it on (or saving it on) re-validates and is refused with
  "no downstream named X". Switched-off rules do not block deleting a downstream.
- **Reusing an id** that appears in stored rows (re-adding `local` for LiteLLM, or `lemmi` after deleting
  it) is allowed; that history then shows under the new downstream's name and colour. The wizard says so
  ("history from an earlier `local` will show under this one").
- **Prices entered or changed later:** rows recorded while the downstream was unpriced are filled in at
  the new prices (as Valar's backfill works today, made per downstream); rows that already have a cost
  keep it.
- `name` is a reserved id too (it's a `/router` sub-command).

## Non-goals

- Non-Anthropic API formats (OpenAI chat, etc.). A downstream must accept Anthropic `/v1/messages`.
- Per-model manual prices, or prices per completion window for non-Valar services.
- Editing Anthropic.
- Sending a test message from the wizard.

## Anti-goals

- Valar looking or behaving differently than today after the migration.
- A key ever reaching the browser, a log line, or `console.json`.
- Routing silently changing on upgrade or delete (a rule quietly sending traffic somewhere else).

## Left to the builder

- Palette and curve geometry for the yard, as long as one downstream looks like today's Valar line.
- How the wizard is presented (modal or inline panel) and its copy.
- File layout and API shapes, within the constraints above.
- How the plugin fetches the mode list (the existing lever API is the natural place).
