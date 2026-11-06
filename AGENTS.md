# AGENTS.md

How to work on signaller. `README.md` describes what it does; this file
covers how to change it without breaking it.

## The one rule that matters most

The router is on the path of every Claude Code session on this machine,
**including the one you may be running in.** If it breaks, every session
breaks.

- **Test on a second router.** Run it on another port with a scratch config
  dir, never on the live 8787:
  ```
  SIGNALLER_CONFIG_DIR=<scratch dir> SIGNALLER_LISTEN=127.0.0.1:8788 python3 signaller.py
  ```
  To test with real traffic, copy `console.json` into the scratch dir. Never
  copy `downstreams.keys.json` or `stats.db`. If a test needs a key, have the
  user add it through the scratch router's console.
- **The live router is the user's.** Don't stop, rebuild or restart the
  container unless they ask; a rebuild cuts off every session for a moment.
- **Never fail closed.** Broken stats, a bad config or a classifier timeout
  must not cost Claude Code its router. The pattern is a fallback plus a banner
  (see `NullStore` and `load_console_config`).

## Stats database

- **Never open the live `stats.db` from the host** with `sqlite3` or any other
  client. A host read of the WAL-mode database through a shared folder corrupted
  it once.
- **Where it lives:** in Docker it is on the `signaller_stats` volume (`/data`), on the
  VM's own disk. Keep it off bind mounts. SQLite over virtiofs corrupted it
  again in normal use (2026-11-03).
- **To inspect it:** use the API, or run a read-only connection inside the
  container:
  ```
  docker exec signaller-router-1 python3 -c 'import sqlite3; c = sqlite3.connect("file:/data/stats.db?mode=ro", uri=True); print(c.execute("PRAGMA quick_check").fetchone())'
  ```
  Or copy it out first: `docker compose cp router:/data/stats.db <dest>`.
- **To repair it:** stop the router and copy the files out. Run `sqlite3 <copy>
  ".recover" | sqlite3 recovered.db` on the copy, check the counts, put the
  result back in the volume, then start the router.
- **Adding a column:** put it in the fresh-database `SCHEMA`, in `COLS` and in
  the `_migrate` types map, so old databases get it on open. No backfills.
- **All writes go through the store's single writer thread** (`Store._q`).
  Anything that deletes or rewrites rows, like `forget_session`, goes through
  that queue so it can't race the prompt delta chain.

## Routing invariants

- **Routing reads the latest human text** (`latest_human_text`), never the
  last message. In a tool loop that text is the same on every step, so the
  turn, its tags, the classifier cache and `turn_key` all hold for the whole
  turn. Step-only details (`Features.step`) are for display.
- **Subagent and teammate** are separate rule fields. `subagent` is the
  `cc_is_subagent=true` marker in the first system block; `teammate` is a
  request with Claude Code's `x-claude-code-agent-id` header (sent for every
  agent but the main thread, which is the team lead in a team) and no marker.
  Both are undocumented, so a Claude Code update can break them.
- **Tags** count only at the very start of the prompt (`lead_tags`).
  `strip_lead_tags` removes them from every user prompt before the body goes
  anywhere; the doubling shadow strips its own copy. The console shows the
  prompt as sent (`untagged`).
- **The one fallback:** a downstream that answers 400 about images, on a
  request that carries one (`has_images`), is retried on Anthropic from the
  original body (`orig_body`) and the person's own headers. The session gets a
  notice (`App.notices`, served on `/ui/api/lever`), which the plugin turns
  into a transcript line and a band above the prompt. Nothing is remembered
  between requests, because a rewound or forked conversation may have no
  image. Every other error passes through as it came.
- **One request prep for every track:** `prepare()` in `signaller.py`
  renames the model, applies Valar's tool fix, swaps credentials and sets the
  window, for the served request and its shadow alike. Change it there, once.
- **Settings lines** (`config.settings`, `overrides.py`) run inside that same
  `prepare()`, after the track is chosen: they change the window and model for
  that downstream, served and shadow alike, ignore the lever, and never touch
  an Anthropic request. First matching line per key wins (`resolve`). A
  window the final model isn't priced for falls back to Now (the same rule the
  Tracks row applies); the hour a turn uses is pinned at its first request
  (`App.hour_of`), so a tool loop crossing an hour boundary doesn't switch
  mid-turn. The plugin's `/signaller <downstream> window|model …` writes through
  `set_settings_for_session` (inserted at the top, under the lock), and
  `prune_settings` drops session ids unseen for a week — an id with no
  sessions row counts as unseen (forgotten sessions go too).
- **Session levers** persist in `session_levers.json`, written through
  `write_json` under the lock by `set_session_lever`. `lever_for` serves only a
  lever the active setup can still follow (a restart or a preset switch may
  have dropped the track it named); the raw entry is kept. `prune_levers`
  drops session ids unseen for a week, the same rule as `prune_settings`.
- **Presets:** `console.json` is the active preset's config; `presets.json`
  holds the rest. `activate_preset` runs under `_rlock` and goes through
  `_put`, so the rev bumps and a page holding an older config gets 409 rather
  than overwriting the switched-in preset. A saved or imported preset carries
  `presets.portable(config)`: no machine state (`seeded`, `migrated_off`) and
  no settings lines bound to a session; anything unparseable is kept for
  `config_from_json` to reject with a reason. An unreadable `presets.json`
  falls back to a fresh "my setup" with a banner, never a dead router.
- **The body is rewritten only when something changes** (leading tags, a
  downstream's model name, Valar's tool fixes): `req` is parsed once, edited,
  and re-serialised once. An untouched Anthropic request goes out byte for
  byte.
- **Prices:** Anthropic rates are per tier in `prices.DEFAULT_ANTHROPIC`. A new
  key there must load from an old `prices.json` (`load()` merges defaults).
  Downstream prices stay at the four `PRICE_KEYS`. Valar is priced from its
  `/v1/models` list, per the window the router sent.

## Secrets

- **Where they live:** downstream keys are only in `downstreams.keys.json`
  (0600), keyed by id and then host — the same id at two hosts keeps two keys,
  and `delete_downstream` leaves a key that an inactive preset still needs at
  the same id and host. Never put them in `console.json`, `presets.json`, an
  API response, a log line, a test fixture or a commit.
- **Env files:** `.env.*` files are git-ignored and can hold real keys. Don't
  print them. To check which variables one sets, print names only:
  `grep -oE '^[A-Z_]+=' file`.
- **What is never logged:** request bodies, prompts, headers or keys. A log
  line is `route= reason= model= status= ms= stream= [err=] shadow=` and nothing
  more.

## Code conventions

- **The router** uses the Python 3 standard library only. No dependencies, no
  build step. It runs as `python3 -E -s`.
- **The console** (`ui/`) is vanilla JS and CSS with no framework and no
  bundler. It is served under a strict CSP (`default-src 'self'`):
  - no inline styles or scripts
  - set dynamic colours with `style.setProperty("--c", …)`
  - no external URLs; `tests/test_console.py::test_page_assets_local_only`
    checks this
  - fonts are local in `ui/fonts/`
- **The console API** is under `/ui/api/`. Every PUT, POST or DELETE needs a
  loopback `Origin` (the CSRF check). `PUT /ui/api/config` also needs the
  body's `rev` to equal `state`'s `config_rev` — a page saving a config that
  changed elsewhere (a `/signaller` write, a prune) gets 409 and reloads, rather
  than erasing those lines. When testing with curl, send
  `-H 'Origin: http://127.0.0.1:<port>'`.
- **The config** (`console.json`) is version 4. If you change its shape, add a
  migration in `routing.config_from_json`, plus a fixture under
  `tests/fixtures/`.
- **Tracks:** Anthropic plus `RoutingConfig.downstreams`. Don't hardcode
  "valar" outside the `kind == "valar"` extras and the defaults.
- **Small diffs:** reuse helpers already in the code (`usd`, `tok`, `priced`,
  `NAME`, `label`, `el`, `api`). Mark deliberate shortcuts with a `ponytail:`
  comment that names the limit.

## Tests

```
pytest -q tests                        # must stay green; about 270 tests, under 30 s
claude plugin test plugin/signaller # the plugin's gate test
node --check ui/app.js                 # the console has no unit tests; at least parse it
```

- **New logic gets one test.** A store change gets one in
  `tests/test_store.py`; an API change gets one in `tests/test_console.py`
  (real HTTP against a fixture app).
- **UI changes:** check them in a browser against the scratch router. The page
  is the only test of the UI. Seed a scratch `stats.db` through
  `store.Store(...)` in a short Python script, not by copying the live one.

## Plugin

- **Where it is:** `plugin/signaller/` (TypeScript hooks module). Bump the
  version in `.claude-plugin/plugin.json` when it changes; users update with
  `/plugin`.
- **The `signaller_lever` tool:** it only switches when the user typed
  `/signaller <mode>`, and each one typed grants one switch, used in the order
  typed (a prompt with any `/signaller` step replaces the queue; one without, or
  one that only quotes `/signaller run` or `name`, keeps it). The queue lives in
  the plugin's store under the session id, so a reload or restart keeps it. Subagents can't switch. The plugin's own refusal text quotes the command,
  so pasting it back grants nothing. Keep that gate.
  Claude must never switch the router on its own judgment.
- **Saved prompts** (`config.prompts`, the console's Prompts section) reach the
  plugin on the lever answer. `/signaller run <name>` only fills the prompt
  box: the person sends it, so its `/signaller` steps are theirs. Don't make it
  submit on its own (a command can't anyway: it holds the turn).

## Git

- **Commit and push only when the user asks.** Work on `master` is fine for
  small changes the user requested; larger features go on a branch or worktree.
- **The message** ends with the attribution line the session gives.
- **Don't commit** `.playwright-mcp/`, `.superpowers/`, scratch databases or
  env files.
