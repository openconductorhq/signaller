# Downstreams Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the hardcoded Valar/Local tracks with a list of downstreams the person adds, edits and deletes from the console (wizard included), with Valar migrated as an ordinary downstream that keeps its look and extras, and Local removed.

**Architecture:** `RoutingConfig` gains `downstreams: tuple[Downstream, ...]` (console.json v3) and every place that names `valar`/`local` reads that list instead; Valar's extras key off `Downstream.kind == "valar"`. Keys live in a separate 0600 file the browser never sees. The console UI generates the lever, yard, cost cards, Tracks rows and selects from the list.

**Tech Stack:** Python 3 stdlib (http.server, sqlite3, unittest/pytest), vanilla JS/CSS/SVG in `ui/`, TypeScript Claude Code plugin in `plugin/router-lever/`.

**Spec:** `docs/superpowers/specs/2026-11-02-downstreams-design.md`

## Global Constraints

- Anthropic is fixed: id `anthropic`, never in `downstreams`, never editable or deletable.
- Downstream id: `^[a-z][a-z0-9-]{0,23}$`; reserved: `anthropic`, `rules`, `reset`, `name`, and every rule-language keyword (`when and or not is contains matches go` and field names `text tag tier model tokens subagent servertool label`). Fixed after creation.
- At most 6 downstreams.
- A downstream must accept Anthropic `/v1/messages`; trailing `/v1` stripped from `url`.
- Keys: `downstreams.keys.json` in the config dir, mode 0600, `{id: key}`. Never in `console.json`, any API response, or any log line.
- Valar detection: `/v1/models` payload where `parse_valar` returns a non-empty dict, or URL host `api.valarhq.ai`.
- Env seed once: `VALAR_URL`/`VALAR_API_KEY` only when `seeded` is false; then `seeded: true` is saved.
- Manual prices: USD per 1M tokens, keys `in`, `out`, `cache_read`, `cache_write`; all-blank = unpriced.
- Valar keeps colour `#2F6DB5`; palette for new downstreams (in order, skipping used): `#1F8A5B #6B5BD6 #C8412F #8A6D1F #1F7A8A #A0418A`.
- No new dependencies.
- Run everything with `pytest -q tests` (unittest-style and pytest-style tests both live there) and `claude plugin test plugin/router-lever`.

## Review Focus

1. **Upgrading a real v2 console.json** (the person's has Valar models, windows, Tags default text, tier rule `go valar`): it must load with zero routing change except Local's removal, never land in `console.json.bad`. → Task 1 test `test_v2_real_config_migrates_without_routing_change`.
2. **A key arriving anywhere outside the keys file** (state, config PUT echo, models/probe responses, error text). → Task 5 test `test_no_key_in_any_response`.
3. **Deleting a downstream a session lever (memory only, set by `/router`) points at**: refused with the reference named. → Task 5 test `test_delete_blocked_by_session_lever`.
4. **Probe of a URL that hangs, redirects, or returns HTML/non-JSON**: wizard gets a reason, router doesn't hang past the 5 s timeout or follow redirects. → Task 5 test `test_probe_bad_endpoints`.
5. **Stored rows for a removed id (`local`, a deleted `lemmi`)** in summary/departures: shown, not crashing, not counted as a configured downstream's cost card. → Task 4 test `test_summary_includes_removed_tracks`.

---

### Task 1: Downstream model, config v3 and migration (routing.py, rulelang.py)

**Files:**
- Modify: `routing.py` (TRACKS, RoutingConfig, DEFAULT_CONFIG, `_track`, `config_from_json`, `config_to_json`), `rulelang.py:185-187`
- Test: `tests/test_routing.py`, `tests/test_rulelang.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Downstream: id: str; name: str; url: str; kind: str  # "valar"|"generic"; models: dict  # tier -> model id, all of MODEL_TIERS; color: str; windows: dict | None  # tier -> WINDOWS value, valar only; prices: dict | None  # {"in","out","cache_read","cache_write"} floats, generic only`
  - `RoutingConfig` loses `models`, `windows`; gains `downstreams: tuple[Downstream, ...]`, `seeded: bool`, `migrated_off: tuple[str, ...] = ()` (names of rules switched off by migration; drives the banner, not persisted).
  - `tracks(cfg) -> tuple[str, ...]` = `("anthropic",) + tuple(d.id for d in cfg.downstreams)`
  - `downstream(cfg, id) -> Downstream | None`
  - `valid_id(id: str, taken: Iterable[str]) -> str | None` — error text or None.
  - `references(cfg, id, session_levers: Mapping[str, str]) -> list[str]` — human-readable references blocking delete ("the lever", "session 819a0a39's lever", "rule 'Tags in your message' line 2", "Otherwise", "classifier label 'coding'", "classifier upstream", "side by side: valar → anthropic"); switched-off rules excluded.
  - `track_model(model, ds: Downstream) -> str`, `track_window(model, ds: Downstream) -> str | None` (None unless kind valar).
  - `ClassifierSettings.upstream` may be `"none"`.
  - `rulelang.parse` accepts any word after `go` (syntax only); returns it lowercased.
- Migration v2 → v3 (inside `config_from_json`), per spec "Migration": valar downstream from v2 `models.valar` + `windows.valar`, url `https://api.valarhq.ai` (Task 2 seeds the real url/key), colour `#2F6DB5`, `seeded: false`; lever/otherwise/labels/upstream/shadow re-pointing; Tags default text loses `when tag is local go local`; other rules with a `go local` line → `enabled: False`, names in `migrated_off`.
- Validation: enabled DSL rules' `go` targets, otherwise, labels, shadows, lever must be in `tracks(cfg)`; classifier upstream in downstream ids or `"none"`; ≤ 6 downstreams; unique ids via `valid_id`; `kind valar` requires `windows` for every tier; generic `prices` either None or four non-negative numbers.
- `DEFAULT_CONFIG`: one Valar downstream (today's models, all windows `asap`), tier rule and labels as today, shadow `{"anthropic": "valar", "valar": "anthropic"}`, Tags text without the local line, `seeded: False`.

- [ ] **Step 0: Fixture:** `mkdir -p tests/fixtures && cp ~/.config/claude-router/console.json tests/fixtures/console_v2.json` (the person's real v2 config; it holds no keys). Main-thread requests only in the routing assertions below (its tier rule also has `subagent` lines).
- [ ] **Step 1: Write failing tests** in `tests/test_routing.py`:
  - `test_v2_real_config_migrates_without_routing_change`: load `tests/fixtures/console_v2.json` (Step 0); assert `[d.id for d in cfg.downstreams] == ["valar"]`, `cfg.downstreams[0].models["opus"] == "zai-org/GLM-5.3"`, Tags text `== "when tag is planning go anthropic\nwhen tag is execution go valar"`, `cfg.migrated_off == ()`, and `route()` of an opus/sonnet/haiku/`#planning` request gives the same track as before (`anthropic`, `valar`, `valar`, `anthropic`).
  - `test_v2_local_references_repointed`: v2 with lever `local`, otherwise `local`, label track `local`, upstream `local`, shadow `anthropic: local`, a custom rule `when tag is x go local` → lever `rules`, otherwise `anthropic`, label `anthropic`, upstream `valar`, shadow `anthropic: None`, custom rule `enabled is False`, `migrated_off == ("<that rule's name>",)`.
  - `test_v3_round_trip`: `config_from_json(config_to_json(cfg)) == cfg` for DEFAULT_CONFIG plus a generic `lemmi` downstream with all tiers `deepseek-v4.1-flash` and prices None.
  - `test_unknown_track_in_enabled_rule_rejected` (`ValueError` containing `no downstream named lemmi`) and `test_unknown_track_in_disabled_rule_allowed`.
  - `test_valid_id`: `None` for `"lemmi"`; error for `"Lemmi"`, `"anthropic"`, `"name"`, `"go"`, `"tier"`, `"a"*25`, and `"lemmi"` when taken.
  - `test_references`: config whose lever, a rule line, otherwise, a label, the upstream and a shadow name `lemmi`, plus `session_levers={"819a0a39-…": "lemmi"}` → seven entries; a disabled rule naming it adds none.
  - `test_seven_downstreams_rejected`.
  - `test_zero_downstreams`: a v3 config with `downstreams: []`, lever `rules`, no rule naming a downstream, classifier upstream `"none"`, shadow `{"anthropic": None}` loads; `tracks(cfg) == ("anthropic",)`.
- In `tests/test_rulelang.py`: `test_go_any_word` (`parse("when tag is x go lemmi")[0].track == "lemmi"`), and `go` followed by nothing or a quoted string still errors.

- [ ] **Step 2: Run** `pytest -q tests/test_routing.py tests/test_rulelang.py` — new tests FAIL.
- [ ] **Step 3: Implement** the interfaces above. `TRACKS` stays only as `("anthropic",)` if something still needs a constant; callers move to `tracks(cfg)`.
- [ ] **Step 4: Update existing tests** in these two files that build `models=`/`windows=` or name `local` to the v3 shape; run until `pytest -q tests/test_routing.py tests/test_rulelang.py` passes.
- [ ] **Step 5: Commit** `feat: downstreams in the routing config (v3), Local migrated away`

### Task 2: Keys file and the one-time env seed

**Files:**
- Create: `keys.py`
- Modify: `claude_router.py` (Config, `load_config`, App init), `env.example`, `compose.yaml`
- Test: `tests/test_keys.py`, `tests/test_proxy.py`

**Interfaces:**
- Produces `class Keys: __init__(self, config_dir: str); get(self, id: str) -> str; set(self, id: str, key: str) -> None; delete(self, id: str) -> None; has(self, id: str) -> bool`. Writes atomically (tmp + `os.replace`), file mode 0600, missing/corrupt file reads as empty (corrupt one renamed `.bad`, like console.json).
- `Config` loses `litellm_url`, `litellm_key`, `valar_url`, `valar_key`; keeps an `env_valar: tuple[str, str]` (url, key) used only by the seed.
- `App.seed(self) -> None`: when `not routing.seeded`, set the valar downstream's url (if env gave one) and `Keys.set("valar", key)` (if given), then save config with `seeded=True`. A set `LITELLM_URL`/`LITELLM_API_KEY` prints one line `LITELLM_* is no longer used; add LiteLLM as a downstream in the console`.
- `App.keys: Keys`.

- [ ] **Step 1: Failing tests:** `tests/test_keys.py::test_round_trip_and_mode` (set/get/delete, `oct(os.stat(p).st_mode & 0o777) == "0o600"`), `test_corrupt_file_reads_empty`; `tests/test_proxy.py::test_env_seeds_valar_once` (App with env key `k1` → `keys.get("valar") == "k1"` and `routing.seeded`; delete valar via config, restart App with the same env → still no valar downstream).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Remove `LITELLM_URL` from `compose.yaml` and the `LITELLM_*` lines from `env.example`.
- [ ] **Step 4: Run** `pytest -q tests/test_keys.py tests/test_proxy.py::test_env_seeds_valar_once` — PASS.
- [ ] **Step 5: Commit** `feat: downstream keys file; Valar seeded from env once`

### Task 3: Proxy, doubling and classifier on downstreams

**Files:**
- Modify: `claude_router.py` (`upstream`, `KEY_VAR`, request path at ~269-310, startup line), `doubling.py:125-216`, `classifier.py`
- Test: `tests/test_proxy.py`, `tests/test_doubling.py`, `tests/test_classifier.py`

**Interfaces:**
- Consumes: Task 1 `downstream()`, `track_model`, `track_window`; Task 2 `App.keys`.
- Produces `upstream(app, track: str) -> tuple[str, str, str, Downstream | None]` (display name, base url, key, downstream); anthropic returns `("Anthropic", cfg.anthropic_url, "", None)`.
- Valar extras (window header, `hoist_inline_tools`, classifier `reasoning_effort: "none"`) when `ds.kind == "valar"`, never `track == "valar"`.
- A route to a downstream with no key → 502 `claude-router: <name> has no key; set it in the console`.
- `Doubler(store, open_upstream, ...)`: caps created lazily per track, 2 each.
- `Classifier(resolve: Callable[[str], tuple[str, str, str] | None])` — upstream id → (url, key, kind); `upstream == "none"` → no result (rule decides nothing).

- [ ] **Step 1: Failing tests:** `test_proxy.py::test_generic_downstream_passthrough` (fake upstream; downstream `lemmi` generic, all tiers `deepseek-v4.1-flash`; lever `lemmi`; request `claude-opus-5-5` → fake sees `Authorization: Bearer <key>`, body model `deepseek-v4.1-flash`, no `X-Valar-Completion-Window`); `test_valar_kind_keeps_window_and_hoist` (existing window/hoist tests pointed at a downstream with a non-`valar` id but `kind: "valar"`); `test_no_key_502_names_downstream`; `test_classifier.py::test_upstream_none_decides_nothing`; `test_doubling.py::test_shadow_to_generic_downstream`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.** Delete the LiteLLM branch.
- [ ] **Step 4: Update** remaining tests in these files (positional `Config(...)` at `test_proxy.py:120`, `test_doubling.py:191`; `local` fixtures) and run `pytest -q tests` — PASS.
- [ ] **Step 5: Commit** `feat: route, double and classify through any downstream`

### Task 4: Prices and the store per downstream

**Files:**
- Modify: `prices.py`, `store.py` (summary ~342-380, stats ~484, reprice ~436, NullStore), `claude_router.py` (`refresh_valar_prices` → `refresh_prices`)
- Test: `tests/test_prices.py`, `tests/test_store.py`

**Interfaces:**
- `Prices.cost(ds: Downstream | None, model_out: str, window: str | None, usage: dict) -> float | None`: anthropic (None ds) → 0.0; kind valar → per-model/window list prices keyed by downstream id (`self.listed[id]`, from `models.json` `"listed_prices": {id: parse_valar(...)}`; read old `"valar_prices"` as `listed["valar"]`); generic with `prices` → `(in*i + out*o + cache_read*cr + cache_write*cw)/1e6`; generic without → None.
- `Prices.set_listed(id: str, prices: dict)`.
- `App.refresh_prices()`: hourly, every kind-valar downstream with a key.
- `Store.reprice(prices, downstreams)`: fills `cost IS NULL` rows of every non-anthropic track that has a price source; never changes a non-NULL cost.
- `Store.summary(sessions, t0, t1, now, tracks: Sequence[str])`: `tracks` dict keyed by the given configured tracks **plus** any track present in the span's rows (removed ones get `"removed": True`); `unpriced` counted for every non-anthropic track; `savings`/`saved_would` sum every non-anthropic track. `tally`/`stats` keyed the same way plus `"same"`.

- [ ] **Step 1: Failing tests:** `test_prices.py::test_generic_manual_price` (prices `{"in":1,"out":2,"cache_read":0.5,"cache_write":1}`, usage in 1e6/out 1e6/cr 0/cw 0 → `3.0`), `test_generic_unpriced_is_none`, `test_old_valar_prices_key_loads`; `test_store.py::test_summary_includes_removed_tracks` (rows for `valar`, `local`, `lemmi`; `tracks=("anthropic","valar")` → keys include `local`, `lemmi` with `removed is True`, and savings only from priced rows), `test_reprice_fills_only_null_costs`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Update** existing price/store tests to the new signatures; `pytest -q tests` — PASS.
- [ ] **Step 5: Commit** `feat: price and summarize every downstream`

### Task 5: Console API for downstreams

**Files:**
- Modify: `console.py` (`state`, `/ui/api/models`, `/ui/api/lever`, new routes), `claude_router.py` (App: `add_downstream`, `delete_downstream`)
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Endpoints (same origin/CSRF guard as today):
  - `POST /ui/api/downstreams/probe` `{url, key?, id?}` (`id` = use the stored key when `key` absent) → `{"ok": bool, "reason": str|None, "models": [ids], "kind": "valar"|"generic"}`. Uses `fetch_models` (5 s timeout, no redirects).
  - `POST /ui/api/downstreams` `{id, name, url, models, prices?, key?}` → `201` + the downstream JSON (no key). Kind and windows (all `asap`) set server-side from a probe; colour from the palette. Errors 400 with `valid_id`/validation text, 409 at 6.
  - `PUT /ui/api/downstreams/<id>/key` `{key}` → `{"key_set": true}`.
  - `DELETE /ui/api/downstreams/<id>` → `200 {"ok": true}`, or `409 {"error": "...", "references": [...]}` from `references(cfg, id, app.session_levers)`; deletes the key too.
  - Edits (name, models, windows, prices) go through the existing `PUT /ui/api/config` (downstreams are in the config JSON; `kind`, `color`, `id` changes rejected).
  - `state` → `"downstreams": {id: {"key_set": bool, "url": host}}` replaces `"upstreams"`; `"banner"` includes migrated-off rule names.
  - `/ui/api/models?track=<any downstream id>`; windows only for kind valar.
  - `/ui/api/lever` GET/PUT responses gain `"modes": ["rules", "anthropic", *ids]`; PUT validates against it.

- [ ] **Step 1: Failing tests:** `test_add_probe_delete_downstream` (probe a fake `/v1/models` with one model → `kind generic`; POST `lemmi`; state shows it with `key_set`; lever PUT `lemmi` works; DELETE while lever is `lemmi` → 409 naming "the lever"; lever back to rules; DELETE → 200; state no longer has it; keys file has no `lemmi`), `test_probe_detects_valar` (fake list with `pricing` → `kind valar`), `test_no_key_in_any_response` (set key `sk-SECRET`; GET state, PUT config echo, models, probe, lever, a 400 and a 409 — `b"sk-SECRET"` in none), `test_delete_blocked_by_session_lever`, `test_probe_bad_endpoints` (fake that sleeps 10 s → ok False within 6 s; 302 → ok False; HTML 200 → ok False, reason mentions not a model list), `test_lever_modes`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `pytest -q tests` — PASS.
- [ ] **Step 5: Commit** `feat: console API to add, probe and delete downstreams`

### Task 6: Plugin modes from the router

**Files:**
- Modify: `plugin/router-lever/hooks/register.ts`, `plugin/router-lever/.claude-plugin/plugin.json` (0.5.0), `plugin/router-lever/tests/gate.test.ts`

**Interfaces:**
- Consumes: Task 5 `modes` in lever responses.
- `MODES` becomes the last `modes` the router returned (seeded `['rules','anthropic']` until the first answer); used by `/router`, the `router_lever` tool's validation, and the `/router <mode>` grant. The tool's `inputSchema` drops the `enum` (modes change at runtime); its description lists "rules, anthropic, or a downstream's id".

- [ ] **Step 1: Failing test** in `gate.test.ts`: fake lever response with `modes: ['rules','anthropic','lemmi']`; after a prompt `do /router lemmi`, the tool call `lever: 'lemmi'` succeeds; `lever: 'local'` → deny `Unknown lever`.
- [ ] **Step 2: Run** `claude plugin test plugin/router-lever` — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** plugin test and `claude plugin validate plugin/router-lever` — PASS.
- [ ] **Step 5: Commit** `feat(plugin): /router modes come from the router's downstreams`

### Task 7: Console UI generated from downstreams

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/app.css`

**Interfaces:**
- Consumes: Task 5 state (`config.downstreams`, `downstreams` key status), endpoints.
- Each downstream's colour reaches CSS as `style="--c:<color>"` on its elements; the hand-written `.v`/`.l`/`--val`/`--loc` rules become `[data-t]` rules using `var(--c)`; Anthropic keeps `--anth`.
- Generated from `cfg.downstreams`: lever buttons (after Anthropic and Rules), yard lines (Anthropic on top; downstream lines spread below; with only Valar, its path is today's straight line at y=125), money cards, Tracks rows (Valar with its window pickers when kind valar; generic with dropdown or text inputs and a prices block), Otherwise / label / classifier upstream (+ "none") / shadow selects, rule syntax highlighting of `go <id>`, departures and doubled-turn colours. Removed tracks in history show their id in `--off` grey with "removed".
- Remove the `localWarn` copy's "local" wording: it now names the downstream ("Conversations that run on <name> may fail when you switch back…").

- [ ] **Step 1:** Generate the lever, yard, cards and Tracks rows from the list; delete the static per-track markup.
- [ ] **Step 2:** Verify with the browser (Playwright) against a router running Tasks 1–5 with the migrated real config: yard, lever and Valar row look as before (compare against a screenshot taken before Task 1 at the same viewport); no console errors besides the favicon 404.
- [ ] **Step 3:** Generate the selects, highlighting, colours in departures and doubled turns; verify a rule `go lemmi` highlights in lemmi's colour.
- [ ] **Step 4:** `pytest -q tests` (console tests serve the page assets) — PASS.
- [ ] **Step 5: Commit** `feat(ui): console draws every downstream from the config`

### Task 8: The wizard and delete in the console

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/app.css`

**Interfaces:**
- Consumes: Task 5 `probe`, `POST`/`DELETE` downstreams, `PUT key`.
- "+ Add downstream" in the Tracks panel (disabled with a reason at 6). Steps per spec: Connect (name → suggested id, editable; URL; key; Test connection showing ok/reason, model count, "looks like Valar"), Models (dropdowns or text; "use one model for every tier", pre-applied when exactly one model is listed), Cost (generic only, four optional fields), Save. A failed test can still be saved. Reusing an id seen in history shows "history from an earlier <id> will show under this one" (the state's summary already lists removed ids).
- Tracks row: edit name/models/windows/prices via config PUT; "Replace key" field (empty keeps); "Delete" with a confirm; a 409 shows its `references` as a list.

- [ ] **Step 1:** Build the wizard and row actions.
- [ ] **Step 2: Verify end to end in the browser against the real lemmi** (URL `https://lemmi.wilfoaware.com`, key from `.env.lemmi`'s `env.ANTHROPIC_AUTH_TOKEN`; paste it into the wizard via Playwright, never echo it): probe lists `deepseek-v4.1-flash` (kind generic); one-model shortcut fills all tiers; save; lever button "lemmi" appears; `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "do /router lemmi, then say pong"` with `--plugin-dir plugin/router-lever` routes to lemmi (departure shows lemmi, status 200); delete while the session lever points at lemmi → refused naming it; `/router reset`; delete → gone from lever, yard, Tracks, keys file.
- [ ] **Step 3:** `pytest -q tests` and `claude plugin test plugin/router-lever` — PASS.
- [ ] **Step 4: Commit** `feat(ui): wizard to add a downstream; delete from the Tracks row`

### Task 9: Docs and deploy

**Files:**
- Modify: `README.md` (overview diagram, defaults table, env vars, `/router` modes, console sections, cost, manual test steps that use local), `docs/superpowers/specs/2026-11-02-downstreams-design.md` (status line)

- [ ] **Step 1:** Update the README: downstreams section (wizard, delete rules, keys file, Valar extras by kind), remove LiteLLM/Local, `/router <downstream id>`.
- [ ] **Step 2:** Back up `~/.config/claude-router/console.json` to the scratchpad, `docker compose up -d --build`, check `/ui/api/state` loads (no banner other than any migrated-off rules), Valar row and yard look as before, and the router log has no errors (inspect `stats.db` only via the API or `docker exec`, never host `sqlite3`).
- [ ] **Step 3:** `pytest -q tests` — PASS.
- [ ] **Step 4: Commit** `docs: downstreams`
