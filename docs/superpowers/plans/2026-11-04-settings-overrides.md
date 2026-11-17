# Settings Overrides Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an ordered list of *settings lines* that change a downstream's Valar window or model name.
- A line applies per session and/or by hour.
- It is set from the console or from `/router` inside a session.

**Architecture:**
- A new pure module `overrides.py` handles parsing, resolving, collisions and pruning.
- The list is stored structured in `console.json` (v4) as `RoutingConfig.settings`.
- `prepare()` applies it for served and shadow requests alike.
- `Features` gains `session` and `hour`, so conditions use the existing rule language.

**Tech Stack:**
- Python 3 stdlib (router).
- Vanilla JS/CSS (console, strict CSP).
- TypeScript hooks module (plugin).

**Spec:** `docs/superpowers/specs/2026-11-04-settings-overrides-design.md`

## Global Constraints

- **Router:** stdlib only, runs as `python3 -E -s`.
- **Console:** no framework. Under CSP `default-src 'self'` that means:
  - no inline styles or scripts;
  - dynamic colours only via `style.setProperty`.
- **Never fail closed:**
  - an invalid settings list loads as empty, with a banner;
  - a line naming a deleted downstream is skipped;
  - a settings fault never fails a request.
- **An untouched Anthropic request goes out byte for byte.** Settings lines never touch the `anthropic` track.
- **Adding a DB column:** add it to `SCHEMA`, `COLS` and the `_migrate` types map. No backfill.
- **Config changes:** v4 needs a migration in `routing.config_from_json` and a v3 fixture under `tests/fixtures/`.
- **Plugin:**
  - bump `plugin/signaller/.claude-plugin/plugin.json` version (0.5.2 → 0.6.0);
  - keep the typed-command gate: one typed `/router …` grants exactly one write of exactly that command.
- **Never log** prompts, bodies, headers or keys.
- **Reuse helpers:**
  - Python: `rulelang.parse`/`evaluate`/`fields_used`, `routing.tier_of`, `MODEL_TIERS`, `WINDOWS`, `_sid_ok`;
  - JS: `el`, `api`, `label`, `NAME`, `shortLabel`, `fullLabel`, `save`.
- **Shortcuts:** mark deliberate ones with `ponytail:` and name the limit.
- **Test commands:**
  - `/opt/homebrew/bin/pytest -q tests` (green);
  - `node --check ui/app.js`;
  - `claude plugin test plugin/signaller`.
- **Live router:** never touch the one on 8787. UI checks use a scratch router: `SIGNALLER_CONFIG_DIR=<scratch> SIGNALLER_LISTEN=127.0.0.1:8788`.

## Shared decisions (all tasks)

**A settings line** is `overrides.Setting(when: str, cond, downstream: str, tier: str | None, key: str, value: str)`:
- `key` is `"window"` or `"model"`.
- `tier=None` means every tier.
- `cond` is `None` when `when` is blank, which means "always".

**JSON shape** in `console.json` `"settings"`: a list of `{"when": str, "downstream": str, "tier": str|null, "set": "window"|"model", "value": str}`.

**Which tier slot a request uses:** this mirrors `track_window`.
- A `claude-*` model uses `tier_of(model)`.
- `other`, and any non-claude model, use the `sonnet` slot.
- A **model** override never applies to a non-`claude-*` model, matching `track_model`'s passthrough.

**Resolution:** walk the list top to bottom. For each key (`window`, `model`), the first line that matches wins.
- **When a line matches:** all of these hold:
  - same downstream;
  - `tier is None or tier == slot`;
  - `cond is None or evaluate(cond, get)`.
- **What a condition reads:** `Features.get` plus `session` and `hour`.
- **Not allowed:** `label` in a settings condition is rejected at validation, since it would need the classifier.

**Session line:** a line whose `cond` is only `session is X` terms joined by `or`.

---

### Task 1: `session` and `hour` in the rule language and Features

**Files:**
- Modify: `rulelang.py` (FIELDS; new `parse_cond`)
- Modify: `routing.py` (`Features.__init__`: `session=None, hour=None`; `RESERVED_IDS` picks the fields up via `FIELDS` already)
- Modify: `signaller.py` (`handle_any`, after `Features.from_body`: set `f.session = any_sid`, `f.hour = time.localtime(t0).tm_hour`)
- Modify: `ui/index.html` (rule reference lists `session` and `hour`)
- Test: `tests/test_rulelang.py`, `tests/test_routing.py`

**Interfaces:**
- Produces:
  - `rulelang.FIELDS["session"] == "word"`, `FIELDS["hour"] == "number"`;
  - `rulelang.parse_cond(text: str) -> tuple | None`: blank means `None`, else one condition tree. Raises `RuleError` with line 1 and col.
  - `Features.session: str | None`, `Features.hour: int | None`.

- [ ] **Step 1: Write failing tests**

```python
def test_hour_wraps_midnight():
    c = parse_cond("hour >= 22 or hour < 6")
    at = lambda h: evaluate(c, lambda n: h)
    assert at(23) and at(3) and not at(12)

def test_session_is():
    c = parse_cond("session is AbC-1 or session is x2")
    assert evaluate(c, lambda n: "abc-1") and not evaluate(c, lambda n: "zz")

def test_parse_cond_blank_and_error():
    assert parse_cond("  ") is None
    with pytest.raises(RuleError):
        parse_cond("hour >")
```

- [ ] **Step 2:** Add a routing test. A rule `when hour < 7 go valar`, with a `Features` whose `hour = 3`, routes to valar; with `hour = 12` it doesn't. Also run `pytest -q tests/test_rulelang.py tests/test_routing.py` and expect FAIL.
- [ ] **Step 3: Implement.** `parse_cond` reuses `_P(_lex(text, 1), 1).or_()` and errors on trailing tokens ("unexpected text after condition"). Then add the two fields to the reference list in `index.html`.
- [ ] **Step 4:** Run `pytest -q tests` and expect PASS.
- [ ] **Step 5: Commit:** `feat(rules): session and hour fields`

### Task 2: `overrides.py` and config v4

**Files:**
- Create: `overrides.py`
- Modify:
  - `routing.py`:
    - `RoutingConfig.settings: tuple = ()` as the LAST field (after `migrated_off`), so positional constructors stay valid;
    - `config_from_json` v3→v4 (`settings: []`), `config_to_json` writes `"version": 4` and `settings`;
    - `references()` lists `settings line N`.
- Create: `tests/fixtures/console_v3.json` (copy of a saved v3 config)
- Test: `tests/test_overrides.py`, `tests/test_routing.py`

**Interfaces:**
- Consumes: `parse_cond`, `evaluate`, `fields_used`-style walk (Task 1).
- Produces (all in `overrides.py`):
  - `Setting` (frozen dataclass, fields as in Shared decisions);
  - `settings_from_json(items: list, downstreams) -> tuple[Setting, ...]` raises `ValueError("settings line N: …")` when:
    - the downstream is unknown;
    - `set` is `window` on a non-valar downstream;
    - the window value is not in `WINDOWS`;
    - the tier is not in `MODEL_TIERS`/None;
    - the model is empty;
    - the condition doesn't parse;
    - the condition uses `label`.
  - `settings_to_json(settings) -> list`.
  - `slot(model: str) -> str` (tier slot as in Shared decisions).
  - `resolve(settings, ds_id: str, model: str, get) -> dict`: returns `{"window": (value, Setting) | absent, "model": (value, Setting) | absent}`.
  - `collisions(settings) -> list[dict]`: each item is `{"line": j, "by": i, "text": str}` (1-based, `i < j`), where:
    - `text` is e.g. `"line 3 is shadowed by line 1 at hours 0–6"`;
    - the hour part is included only when both conditions are hour-only.
  - `is_session_line(s) -> frozenset | None`: the line's session ids, or None.
  - `prune(settings, last_seen: Callable[[str], float | None], now: float, days=7) -> tuple`:
    - drops ids seen longer than 7 days ago from session lines;
    - an id with unknown `last_seen` is kept (`ponytail:` note);
    - a line left with no ids is removed;
    - rebuilds `when` as `" or ".join(f"session is {id}")`.
  - `upsert_session(settings, sid, ds_id, tier, key, value) -> tuple`:
    - finds a line whose `when` is exactly `session is <sid>` with the same (downstream, tier, key);
    - if found, replaces its value in place;
    - if not, inserts a new line at index 0;
    - `value=None` removes that line.
  - `reset_session(settings, sid, ds_id) -> tuple`: removes `sid` from every session line for `ds_id`, and drops lines left empty.
  - `describe(applied: dict) -> str | None`:
    - JSON: `[{"set": "window", "value": "priority", "when": "hour < 7"}, …]`;
    - None when nothing applied.
- **Disjointness in `collisions`:**
  - two hour-only conditions are disjoint when `{h for h in range(24) if evaluate(c, lambda n: h)}` sets don't intersect;
  - two session lines are disjoint when their id sets don't intersect;
  - anything else counts as overlapping;
  - tiers overlap if either is None or they are equal;
  - a collision also needs the same downstream, same key and different values.

- [ ] **Step 1: Write failing tests** in `tests/test_overrides.py`. One per behaviour:
  - **`test_first_match_wins_per_key`:**
    - lines `[hour<7 window priority, (blank) window standard, (blank) model glm-x]` at hour 3 give `priority` and `glm-x`;
    - at hour 12 they give `standard` and `glm-x`.
  - **`test_tier_line_and_slot`:**
    - a `tier="opus"` line applies to `claude-opus-5-5`, not to `claude-sonnet-5-5`;
    - a model line never applies to `gpt-4`.
  - **`test_no_lines_resolves_empty`:** `resolve((), …) == {}`.
  - **`test_collisions`:**
    - overlapping hour ranges are flagged with "at hours";
    - disjoint hour ranges are not;
    - disjoint session sets are not;
    - a blank condition and a session line are flagged;
    - equal values are not flagged.
  - **`test_validation`:**
    - a window on a generic downstream raises;
    - `label is coding` raises;
    - an unknown downstream raises;
    - a bad window value raises.
  - **`test_prune`:**
    - an id last seen 8 days ago is dropped and one 1 day ago is kept;
    - a line whose ids all go is removed;
    - an unknown id is kept;
    - a non-session line is untouched.
  - **`test_upsert_and_reset`:**
    - the first upsert inserts at the top;
    - a second upsert with the same key replaces it (list length unchanged);
    - a different tier adds another line;
    - `value=None` removes;
    - `reset_session` takes the sid out of a two-session line and leaves the other id.
- [ ] **Step 2:** Add routing tests:
  - `tests/fixtures/console_v3.json` loads with `settings == ()`;
  - `config_to_json` round-trips a settings line;
  - `references(cfg, "valar", {})` includes `"settings line 1"`.

  Run them and expect FAIL.
- [ ] **Step 3: Implement** `overrides.py` and the config changes. In `config_from_json`:
  - accept v3 by adding `settings: []`;
  - accept v4 as is.

  `settings_from_json` failing must fail the config load, and `load_console_config`'s existing fallback plus banner covers that.
- [ ] **Step 4:** Run `pytest -q tests` and expect PASS. Older fixtures (v1, v2) still load.
- [ ] **Step 5: Commit:** `feat: settings lines (overrides.py) in console.json v4`

### Task 3: apply settings in `prepare()`, record them on the row

**Files:**
- Modify: `signaller.py`:
  - `prepare(app, track, model, req, headers, routing=None, f=None)` returns a 9-tuple: the existing 8 plus `override: str | None`;
  - after `track_model`/`track_window`, when `f` is set and `ds` is a downstream, apply `resolve(routing.settings, ds.id, model, lambda n: f.get(n, lambda: "none"))`;
  - wrap the resolve in `try/except Exception`: on failure, no override, and log `settings failed: <Type>` only.
- Modify: `doubling.py` `_run`: pass its `f` to `prepare`, unpack 9 values.
- Modify: `store.py`: `requests.override TEXT` in `SCHEMA`, `COLS`, `_migrate`.
- Modify: `signaller.py` `record_request`: `"override": override`.
- Test: `tests/test_proxy.py`, `tests/test_doubling.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `overrides.resolve`, `overrides.describe` (Task 2); `f.session`/`f.hour` (Task 1).
- Produces:
  - the `requests.override` column, which holds the JSON from `describe`;
  - `prepare(...)[8]`.

- [ ] **Step 1: Write failing tests:**
  - **`test_proxy::test_settings_line_sets_window_and_model`:** with the config line `when session is s-one set valar window priority` plus a blank-condition `model glm-x` line, a request from session `s-one` reaches the upstream with:
    - `X-Valar-Completion-Window: priority`;
    - `model == "glm-x"`.

    The stored row's `window == "priority"`, and its `override` JSON lists both. A request from `s-two` gets the downstream default window.
  - **`test_proxy::test_anthropic_untouched_by_settings`:** an Anthropic-routed request's upstream body is byte-identical to the one sent.
  - **`test_doubling`:** the shadow to valar applies its own lines (`window` header is `priority` for a matching session).
  - **`test_store`:** an old DB gains `override` on open.
- [ ] **Step 2:** Run them and expect FAIL.
- [ ] **Step 3: Implement.** Make these changes:
  - **Applying an override:** an applied `model` sets `req["model"]` and `changed=True`. An applied `window` replaces the header value.
  - **Pricing:** it keeps using the returned `out_model`/`window`.
  - **Passthrough:** with `f=None`, nothing changes.
- [ ] **Step 4:** Run `pytest -q tests` and expect PASS.
- [ ] **Step 5: Commit:** `feat(proxy): settings lines change the window and model sent, served and shadow`

### Task 4: console API, pruning, time zone

**Files:**
- Modify: `console.py`:
  - `state()` adds:
    - `"collisions": overrides.collisions(app.routing.settings)`;
    - `"clock": {"now": time.time(), "tz": time.strftime("%Z"), "hour": time.localtime().tm_hour}`.
  - New endpoint `PUT /ui/api/settings`:
    - body: `{session, downstream, tier|null, set, value|null}` or `{session, downstream, reset: true}`;
    - checks the loopback Origin like other writes;
    - checks `_sid_ok`;
    - validates through `settings_from_json`, so errors come back as 400 with the message.
    - It returns `{"lines": [<json of lines whose session set contains sid>], "collisions": […]}`.
- Modify: `signaller.py`:
  - `App.set_settings_for_session(...)` runs under `_rlock` and writes through `_put`;
  - `App.prune_settings()` is called once at startup and in the hourly `start_price_refresh` loop;
  - pruning saves only when the list changed, and is skipped when the store is `NullStore`;
  - `last_seen` comes from a new `Store.session_last_seen(ids) -> dict[str, float]` (SELECT id, last_ts FROM sessions WHERE id IN …), which `NullStore` returns as `{}`.
- Modify: `compose.yaml`: mount `/etc/localtime:/etc/localtime:ro` and pass `TZ: ${TZ:-}`. Document in a comment that `hour` uses this zone.
- Test: `tests/test_console.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: `upsert_session`, `reset_session`, `prune`, `collisions` (Task 2).
- Produces:
  - `PUT /ui/api/settings` (as above), used by Task 5;
  - `state.collisions` and `state.clock`, used by Task 6.

- [ ] **Step 1: Write failing tests:**
  - **`test_console::test_session_settings_endpoint`:**
    - a PUT adds a top line;
    - a second PUT with the same key updates it, so there is one line;
    - `reset` removes it;
    - no Origin gives 403;
    - a bad window gives 400;
    - a bad session id gives 400.
  - **`test_console::test_state_has_collisions_and_clock`.**
  - **`test_store::test_session_last_seen`.**
  - **`test_console`** (or a proxy-level test): `App.prune_settings` drops a session whose `last_ts` is 8 days old and saves `console.json`.
- [ ] **Step 2:** Run them and expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `pytest -q tests` and expect PASS.
- [ ] **Step 5: Commit:** `feat(console): per-session settings endpoint, collision list, clock, pruning`

### Task 5: plugin `/router` settings form

**Files:**
- Modify: `plugin/signaller/hooks/register.ts`
- Modify: `plugin/signaller/.claude-plugin/plugin.json` (version `0.6.0`)
- Test: `plugin/signaller/tests/gate.test.ts`

**Interfaces:**
- Consumes: `PUT /ui/api/settings` (Task 4).
- **The command:** `/router <downstream> [<tier>] window|model <value>` and `/router <downstream> reset`.
  - `<tier>` is one of fable, opus, sonnet, haiku.
  - `<downstream>`, `<tier>` and the key are lowercased. `<value>` keeps its case, since model ids are case-sensitive.
  - Existing forms are unchanged: `/router`, `/router <mode>`, `/router reset`, `/router name <text>`.
- **The grant:** keep the existing one-word lever grant. Add a settings grant captured by `/(?:^|\s)\/router\s+([a-z0-9_-]+)\s+(?:(fable|opus|sonnet|haiku)\s+)?(?:(window|model)\s+(\S+)|(reset))/gi`.
  - It is stored as a canonical string `"<ds> <tier|*> <key> <value>"` or `"<ds> reset"`.
  - Each typed command grants one use.
- **The tool:** `router_lever` gains an optional `settings: string` input in the same command form. It is canonicalised the same way and must match an unused grant, or it's denied with the same wording style as levers. Update the tool description.
- **The reply:** the command answers with the resulting lines for this session, e.g. `signaller: valar window priority for this session (line 1)`, and with any collision text.

- [ ] **Step 1: Write failing gate tests:**
  - a tool call with `settings: "valar window priority"` is denied without a typed grant;
  - after the prompt `do /router valar window priority` it is allowed once, then denied again;
  - `/router valar window standard` typed does not grant `priority`;
  - a model value's case is preserved.
- [ ] **Step 2:** Run `claude plugin test plugin/signaller` and expect FAIL.
- [ ] **Step 3:** Implement and bump the version.
- [ ] **Step 4:** Run `claude plugin test plugin/signaller` and expect PASS.
- [ ] **Step 5: Commit:** `feat(plugin): /router <downstream> [tier] window|model <value>, gated`

### Task 6: console panel, row display, docs

**Files:**
- Modify: `ui/index.html`: a "Settings by session and time" section next to the Rulebook, holding:
  - a hint;
  - the clock line `Router time 14:05 (IST)`;
  - a list, a `+ Add line` button and a collision list.
- Modify: `ui/app.js`:
  - **Rows:** one per line with:
    - a condition input (session ids shown with `shortLabel` in a hint under it);
    - a downstream select;
    - a tier select (`every tier` plus the tiers);
    - a setting select (window only offered for valar-kind);
    - a value control: a window select from `windowsFor`, or a model picker reusing `modelPick`;
    - up, down and delete buttons.
  - **Saving:** through `save({...cfg, settings})`.
  - **`+ Add line`:** pre-fills `when` with `session is A or session is B` from `view.pick`, or blank if none is picked.
  - **Collisions:** shown under the list, with both line numbers marked.
  - **`depRow`:** when `r.override` is set, show `window: priority` / `model: glm-x` next to the reason, with each line's `when` in `title`.
- Modify: `ui/app.css`: minimal rules for the new section, using existing tokens.
- Modify: `README.md` (console section and `/router` usage), `AGENTS.md` (routing invariants: settings lines run in `prepare()` after the track is chosen, ignore the lever, and never touch Anthropic).
- Test: `node --check ui/app.js`; a browser check against a scratch router (port 8788) with a scratch `stats.db` seeded through `store.Store(...)`, rows carrying `override` JSON.

- [ ] **Step 1:** Implement the panel and the row display.
- [ ] **Step 2:** Run `node --check ui/app.js` and `pytest -q tests` and expect PASS.
- [ ] **Step 3: Browser check** on the scratch router. Confirm that:
  - adding a line with picked sessions pre-fills them;
  - a window select offers windows only on valar;
  - reorder and delete save;
  - a collision shows;
  - the clock shows;
  - a seeded row shows `window: priority` with its tooltip.

  Screenshots go under `.playwright-mcp/` and get deleted afterwards.
- [ ] **Step 4:** Update README and AGENTS.
- [ ] **Step 5: Commit:** `feat(ui): settings lines panel and per-row override; docs`

## Review Focus

These are failure modes the spec implies but no task test pins. Where needed, each one is added to its owning task's tests.

1. **A session id with uppercase letters.** `evaluate` lowercases both sides, so `session is AbC` must match the header `abc` and `AbC` alike. Task 1's `test_session_is` covers it.
2. **A settings line naming a downstream that was later deleted.** Deletion is blocked by `references()` (Task 2's test). A hand-edited `console.json` must load with a banner, not crash: Task 2 adds `test_bad_settings_falls_back` through `load_console_config`.
3. **A non-claude model id on Valar** (a direct model name). The model override must not rename it, though the window still applies. Task 2's `test_tier_line_and_slot` covers this.
4. **Hour taken from UTC in Docker.** This is visible only through the clock line. Task 4's state test asserts `clock.hour == time.localtime().tm_hour`.
5. **`/router valar` with no further words** must stay the lever switch, not a settings form. Task 5 adds a gate test that `/router valar` still switches the lever.
