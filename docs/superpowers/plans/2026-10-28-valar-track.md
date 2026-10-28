# Valar Track Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Valar as a third track (anthropic / valar / local) to the console router. Make each track's models editable on the page, the classifier's upstream selectable, and doubling choose a shadow track per served track.

**Architecture:** The track list grows from 2 to 3 in `routing.py`/`rulelang.py`; the config file goes to version 2 (v1 migrates on load). `claude_router.py` gets one helper that maps a track to (name, url, key), shared by the proxy, doubling and the console. Valar and LiteLLM go through the same code path (key swap + `track_model` rewrite). The page grows a Tracks panel and three-way controls.

**Tech Stack:** Python 3 standard library only, `unittest`; vanilla JS/CSS in `ui/` (no CDN).

**Spec:** `docs/superpowers/specs/2026-10-28-valar-track-design.md` (builds on `2026-10-17-console-design.md`). Mock: `docs/superpowers/specs/2026-10-28-valar-track/cockpit-valar.html`.

## Global Constraints

- Standard library only; no `pip install`; page loads nothing from the internet.
- `TRACKS = ("anthropic", "valar", "local")`; lever values `anthropic | rules | valar | local`.
- Defaults (fresh config):
  - Tags rule: `when tag is planning go anthropic` / `when tag is local go local` / `when tag is execution go valar`.
  - Tier rule: `when tier is opus or tier is fable go anthropic` / `when tier is sonnet or tier is haiku or tier is other go valar`.
  - Classifier labels: planning, design & review → anthropic; coding, chit-chat → valar.
  - Otherwise: anthropic.
- Default models: valar `{"sonnet": "zai-org/GLM-5.3", "haiku": "deepseek-ai/DeepSeek-V4.1-Flash-1017"}`; local `{"sonnet": "claude-sonnet-4-6", "haiku": "claude-haiku-4-5-20251001"}`.
- Default classifier: upstream `valar`, model `deepseek-ai/DeepSeek-V4.1-Flash`, threshold 0.7, timeout 2.5 s. Sends `"reasoning_effort": "none"` only when the upstream is valar.
- Default doubling: `{"rate": 0, "tag": "", "shadow": {"anthropic": "valar", "valar": "anthropic", "local": null}}`. Caps: anthropic 2, valar 2, local 1.
- Env:
  - `VALAR_URL` defaults to `https://api.valarhq.ai`; strip a trailing `/`, then a trailing `/v1`.
  - `VALAR_API_KEY` and `LITELLM_API_KEY` are both optional.
  - `CLAUDE_ROUTER_LOCAL_SONNET`/`_HAIKU` are retired.
- A track whose key is empty answers 502 `claude-router: Valar not configured (VALAR_API_KEY unset)` (LiteLLM: `LITELLM_API_KEY unset`). Never fall back to another track.
- Valar colour `#2F6DB5`.
- Tests: `python3 -m unittest` from repo root. The whole suite stays green after every task, including `test_speed`.

## Review Focus

1. **A v1 `console.json` from the existing (Linux) install** loads, keeps its behaviour (classifier stays on local, doubling directions preserved), and the next save writes v2. Test in Task 1.
2. **The Valar key leaks nowhere:** not in `/ui/api/state`, not in the models response, not in logs, not to Anthropic on a shadow. Tests in Tasks 2 and 4.
3. **Unconfigured or down Valar never reroutes:** 502 on the served request; the classifier stays silent; the shadow records an error. Tests in Tasks 2 and 3.
4. **A non-Claude model id** (`/model zai-org/GLM-5.3`) reaches Valar unchanged and isn't rewritten on a shadow either. Test in Task 2.
5. **`/ui/api/models` from another website** (foreign Origin or Host) → 403; the key is never used. Test in Task 4.

---

## File Structure

- `routing.py`: tracks, tier, config v2 + migration, `track_model`. Task 1.
- `rulelang.py`: `go valar`. Task 1.
- `claude_router.py`: `Config`, `load_config`, `upstream`, `upstream_headers`, proxy path. Task 2.
- `doubling.py`: shadow per served track, caps, model/key per track. Task 2.
- `classifier.py`: upstream selection. Task 3.
- `store.py`, `console.py`: stats, verdicts, state, models endpoint. Task 4.
- `ui/index.html`, `ui/app.js`, `ui/app.css`: page. Task 5.
- `README.md`, `env.example`: docs; live acceptance. Task 6.

---

### Task 1: Three tracks in the routing core and config v2

**Files:**
- Modify: `routing.py`, `rulelang.py`
- Test: `tests/test_routing.py`, `tests/test_rulelang.py`

**Interfaces:**
- Produces:
  - `TRACKS = ("anthropic", "valar", "local")` and `tier_of(model) -> "opus"|"fable"|"sonnet"|"haiku"|"other"`.
  - `ClassifierSettings(model, threshold, timeout_s, labels, upstream: str = "valar")`.
  - `Doubling(rate: float, tag: str, shadow: dict)`, where `shadow` maps each track to a track or `None`.
  - `RoutingConfig(lever, otherwise, rules, classifier, doubling, models: dict)`, where `models = {"valar": {"sonnet", "haiku"}, "local": {"sonnet", "haiku"}}`.
  - `track_model(model: str, m: dict) -> str`: a non-`claude-` id is returned unchanged; a `claude-` id containing `haiku` gives `m["haiku"]`; any other `claude-` id gives `m["sonnet"]`.
  - `config_from_json` accepts versions 1 and 2. `config_to_json` writes version 2.
  - `rulelang.parse` accepts `go valar`.

- [ ] **Step 1: Write the failing tests.** Update every test that builds a `RoutingConfig`, `Doubling` or `ClassifierSettings` by position for the new fields. Add:
  - `test_go_valar` (rulelang): `parse("when tag is execution go valar")[0].track == "valar"`; `go openai` → `RuleError` mentioning `anthropic, valar or local`.
  - `test_tier_fable`: `tier_of("claude-fable-5-1") == "fable"`; `tier_of("zai-org/GLM-5.3") == "other"`.
  - `test_default_rulebook_intent` (classifier stub always returns no label). Expected (track, reason) under `DEFAULT_CONFIG`:

    | Request | Expected |
    |---|---|
    | opus, "hi" | `("anthropic", "rule:Model the request asked for:1")` |
    | fable, "hi" | `("anthropic", "rule:Model the request asked for:1")` |
    | sonnet, "hi" | `("valar", "rule:Model the request asked for:2")` |
    | haiku, "hi" | `("valar", "rule:Model the request asked for:2")` |
    | `zai-org/GLM-5.3`, "hi" | `("valar", "rule:Model the request asked for:2")` |
    | opus, "#execution go" | `("valar", "rule:Tags in your message:3")` |
    | haiku, "#planning x" | `("anthropic", "rule:Tags in your message:1")` |
    | opus, "#local x" | `("local", "rule:Tags in your message:2")` |
    | opus, "#local #planning" | `("anthropic", "rule:Tags in your message:1")` |
    | opus, "#plan x" | `("anthropic", "rule:Model the request asked for:1")` (`#plan` no longer a tag) |
  - `test_classifier_skips_fable`: with the classifier rule enabled and a stub that raises if called, a fable request never calls the stub, and its explanation says `skipped: main session (fable)`.
  - `test_track_model`: haiku → `"H"`; sonnet, opus, `claude-3-5-foo` → `"S"`; `"zai-org/GLM-5.3"` → unchanged (with `m={"sonnet":"S","haiku":"H"}`).
  - `test_v1_migrates`: a v1 dict (copy of the old `config_to_json` output with `when_local=True, when_anthropic=False`) loads with:
    - `models` = the default models;
    - `classifier.upstream == "local"`;
    - `doubling.shadow == {"anthropic": None, "local": "anthropic", "valar": None}`;
    - its own rules untouched.

    `config_to_json` of the result has `"version": 2`.
  - `test_v2_roundtrip`: `config_from_json(config_to_json(DEFAULT_CONFIG)) == DEFAULT_CONFIG`.
  - `test_v2_errors` → `ValueError` for:
    - a missing `models.valar.haiku`;
    - an empty-string model;
    - `classifier.upstream = "anthropic"`;
    - `shadow.valar = "valar"` (same as served);
    - `shadow.valar = "openai"`;
    - `lever = "openai"`;
    - `otherwise = "openai"`.

- [ ] **Step 2: Run, expect failure.** `python3 -m unittest tests.test_routing tests.test_rulelang` → errors on the new fields and names.

- [ ] **Step 3: Implement.**
  - The classifier rule's opus skip becomes `f.tier in ("opus", "fable")`; its outcome text names the tier. `label()` gets the same skip.
  - `_track` error text becomes `must be anthropic, valar or local`.
  - Migration (v1 → v2) happens inside `config_from_json` before validation, exactly as in the test. v1 `classifier` gets `upstream: "local"`; a v1 dict never has `models`.
  - v2 requires `models` with both tracks and both keys as non-empty strings, and `classifier.upstream` ∈ `{"valar", "local"}`. `shadow` must have all three tracks, each value a different track or `None`.
  - Keep `Features.get` and the rule language fields unchanged.

- [ ] **Step 4: Run all tests, expect pass.** `python3 -m unittest` → OK. Fix other suites' fixtures that construct `Doubling(...)`/`RoutingConfig(...)` (they get the defaults' `shadow`/`models`); they must not change meaning. A test asserting the old defaults (`test_defaults_match_v1_with_classifier_on_for_opus`) is rewritten to assert the new defaults; note it in the ledger.

- [ ] **Step 5: Commit** `git commit -am "feat: valar track in routing core, config v2 with v1 migration"`

---

### Task 2: Valar upstream in the proxy and doubling

**Files:**
- Modify: `claude_router.py`, `doubling.py`
- Test: `tests/test_proxy.py`, `tests/test_doubling.py`, `tests/test_routing.py` (config tests)

**Interfaces:**
- Consumes: `TRACKS`, `track_model`, `RoutingConfig.models`, `Doubling.shadow` from Task 1.
- Produces:
  - `Config(listen_host, listen_port, anthropic_url, litellm_url, litellm_key, valar_url="https://api.valarhq.ai", valar_key="")`. `local_sonnet`/`local_haiku` and `local_model` are removed.
  - `upstream(cfg, track) -> tuple[str, str, str]` returns (display name, base url, key):
    - anthropic → `("Anthropic", cfg.anthropic_url, "")`;
    - valar → `("Valar", cfg.valar_url, cfg.valar_key)`;
    - local → `("LiteLLM", cfg.litellm_url, cfg.litellm_key)`.
  - `upstream_headers(headers, key) -> list[tuple[str, str]]`: the renamed `local_headers`, same behaviour.
  - `Doubler`:
    - `pending_served(turn_id, request_id, step, served_track, shadow_track)`.
    - `start_shadow(turn_id, request_id, f, served_track, method, path, headers, body, cfg, models)`.
    - `turn_for` reads `d.shadow.get(served_track)`; `None` means don't double.
    - `PairHandle(..., served_track, shadow_track)`.

- [ ] **Step 1: Write the failing tests.** Fake upstreams A, V, L as in the existing `test_proxy.py` harness. The router starts with `Config("127.0.0.1", 0, A, L, "LK", V, "VK")` and a routing config whose models are `valar {"sonnet": "VS", "haiku": "VH"}` and `local {"sonnet": "S", "haiku": "H"}`. Existing tests switch from positional `"S","H"` to these models.
  - `test_valar_swaps_credentials_and_model`: a sonnet request reaches V with `Authorization: Bearer VK`, no `x-api-key`, `anthropic-beta == "claude-code-20250219"`, body `model == "VS"`; A and L receive nothing. A haiku request → `"VH"`.
  - `test_valar_streams`: the first SSE event through the Valar track arrives in under 0.25 s (same shape as the existing streaming test).
  - `test_non_claude_model_untouched`: model `zai-org/GLM-5.3` → V receives `zai-org/GLM-5.3`.
  - `test_valar_not_configured_502`: router with `valar_key=""` → sonnet request → 502, message `claude-router: Valar not configured (VALAR_API_KEY unset)`; V, A and L receive nothing.
  - `test_valar_down_502`: V on a closed port → 502 with `Valar unreachable`; A receives nothing.
  - `test_local_still_works`: `#local` request → L with `Bearer LK`, model `"S"`.
  - Doubling (`test_doubling.py`):
    - `test_shadow_per_served_track`: shadow `{"anthropic": "valar", ...}` and rate 1, opus request → served on A, shadow on V with `Bearer VK` and model `"VS"`; pair `shadow_track == "valar"`.
    - `test_shadow_none_means_off`: `shadow.valar = None`, sonnet request → no pair.
    - `test_shadow_to_anthropic_keeps_client_auth`: served on valar, shadow on anthropic with the client's own `Authorization`; V never sees the client token. Adapt the existing test of that name.
    - `test_busy_valar_shadow_skips`: cap 2 holds, as in the existing busy test.
  - Config (`test_routing.py`):
    - `test_config_defaults` asserts `valar_url == "https://api.valarhq.ai"` and both keys `""`.
    - `test_config_valar_url_strip`: `https://api.valarhq.ai/v1/` → `https://api.valarhq.ai`.
    - `test_config_keys_optional`: `load_config({})` succeeds.

- [ ] **Step 2: Run, expect failure.** `python3 -m unittest tests.test_proxy tests.test_doubling tests.test_routing`

- [ ] **Step 3: Implement.**
  - `load_config` reads `VALAR_URL` and `VALAR_API_KEY`, and stops requiring `LITELLM_API_KEY`.
  - The handler, for a non-anthropic track:
    - empty key → 502 not-configured;
    - otherwise → `upstream_headers(..., key)`;
    - `out_model = track_model(model, routing.models[route])`, rewriting the body only if it changed;
    - 502 messages use `upstream(cfg, route)` name/url.
  - `open_upstream` picks the url via `upstream()`.
  - `Doubler._run` does the same for the shadow track, using `models`. An empty shadow key records `error: "<Name> not configured"` on the pair.
  - Caps: `{"anthropic": 2, "valar": 2, "local": 1}`, inflight dict for all three.
  - The startup line logs `anthropic=… valar=<url> (key set|no key) litellm=<url> (key set|no key)`.

- [ ] **Step 4: Run all tests, expect pass.** `python3 -m unittest` → OK, including `test_speed`.

- [ ] **Step 5: Commit** `git commit -am "feat: valar upstream in proxy and doubling"`

---

### Task 3: Classifier upstream

**Files:**
- Modify: `classifier.py`, `claude_router.py` (`App`)
- Test: `tests/test_classifier.py`, `tests/test_proxy.py` (classifier wiring)

**Interfaces:**
- Consumes: `ClassifierSettings.upstream` (Task 1), `Config` (Task 2).
- Produces: `Classifier(upstreams: Mapping[str, tuple[str, str]], cache_size=512)`, mapping track → (base url, key). `App` builds it as `{"valar": (cfg.valar_url, cfg.valar_key), "local": (cfg.litellm_url, cfg.litellm_key)}`.

- [ ] **Step 1: Write the failing tests** (the fake chat-completions server from the existing `test_classifier.py`; two instances, FV and FL):
  - `test_upstream_valar`: settings `upstream="valar"` → FV gets the request with `authorization: Bearer VK` and body `reasoning_effort == "none"`; FL gets nothing; label returned.
  - `test_upstream_local`: settings `upstream="local"` → FL gets `Bearer LK`; body has no `reasoning_effort`.
  - `test_not_configured_silent`: valar key `""` → `ClassResult(None, None, "classifier: Valar not configured")`; nothing sent.
  - `test_cache_key_includes_upstream`: the same text classified on valar then local makes two upstream calls.
  - Existing tests change `Classifier(url, "k")` to `Classifier({"local": (url, "k")})` with settings `upstream="local"`.

- [ ] **Step 2: Run, expect failure.** `python3 -m unittest tests.test_classifier`

- [ ] **Step 3: Implement.** The upstream is looked up per call from `s.upstream`. Keep the docstring accurate (no longer "local LiteLLM").

- [ ] **Step 4: Run all tests, expect pass.** `python3 -m unittest`

- [ ] **Step 5: Commit** `git commit -am "feat: classifier upstream selectable (valar default)"`

---

### Task 4: Stats, verdicts, state and the models endpoint

**Files:**
- Modify: `store.py`, `console.py`
- Test: `tests/test_store.py`, `tests/test_console.py`

**Interfaces:**
- Consumes: `TRACKS`, `upstream(cfg, track)` (Task 2).
- Produces:
  - `Store.stats()` adds `valar_pct`; `tally` = `{"anthropic", "valar", "local", "same"}`.
  - `Store.set_verdict` accepts any `TRACKS` value or `"same"`.
  - `/ui/api/state` `upstreams` = `{"anthropic_model_hint": …, "valar": {"url": <host[:port]>, "configured": bool}, "local": {...}}`. It never includes a key.
  - `GET /ui/api/models?track=valar|local` → `{"models": [sorted ids]}`.
  - `console.list_models(url: str, key: str, timeout: float = 5) -> list[str]` fetches `<url>/v1/models` with `Authorization: Bearer <key>` and returns the sorted `data[].id`.

- [ ] **Step 1: Write the failing tests.**
  - Store:
    - `test_stats_three_tracks`: one request per track → each `_pct` is 33 and they sum to ≤ 100.
    - `test_verdict_valar`: `set_verdict(t, "valar")` → True; the tally counts it; `"openai"` → False.
  - Console (fake upstream answering `GET /v1/models` with `{"data":[{"id":"b"},{"id":"a"}]}`):
    - `test_models_endpoint`: `?track=valar` → `{"models":["a","b"]}`; the fake saw `Bearer VK`; the response body doesn't contain `VK`.
    - `test_models_errors`:
      - `?track=anthropic` → 400;
      - `?track=valar` with an empty key → 502 `Valar not configured`;
      - upstream down → 502 with the reason;
      - foreign `Host` → 403;
      - `Origin: http://evil.com` → 403, and the fake saw nothing.
    - `test_state_upstreams_no_key`: `state` has `valar.configured` true/false correctly, and the JSON text contains neither key.
    - `test_verdict_api_valar`: `PUT …/verdict {"verdict":"valar"}` → 200.

- [ ] **Step 2: Run, expect failure.** `python3 -m unittest tests.test_store tests.test_console`

- [ ] **Step 3: Implement.** The models endpoint goes in `api()`. It uses `upstream(app.cfg, track)`, imported late inside the function because `claude_router` imports `console`. Errors use `Err(status, msg)`.

- [ ] **Step 4: Run all tests, expect pass.** `python3 -m unittest`

- [ ] **Step 5: Commit** `git commit -am "feat: three-track stats and verdicts, models endpoint"`

---

### Task 5: The page

**Files:**
- Modify: `ui/index.html`, `ui/app.js`, `ui/app.css`
- Test: manual in-browser check (no JS test harness exists), plus `tests/test_console.py` static check.

**Interfaces:**
- Consumes: state/config/models API from Task 4; config shape from Task 1.

- [ ] **Step 1: Write the failing test** `test_page_has_valar` in `tests/test_console.py`: `GET /ui/` body contains `data-v="valar"` (lever) and `id="tracks"`; `GET /ui/app.js` contains `/ui/api/models`.

- [ ] **Step 2: Run, expect failure.** `python3 -m unittest tests.test_console`

- [ ] **Step 3: Implement**, following the mock (`cockpit-valar.html`):
  - `NAME`/`trk`/colour classes: add valar (`.to-v`, `.v`, `--val:#2F6DB5`); replace `other()` with the pair's `shadow_track`.
  - Lever: 4 radios (Anthropic · Rules · Valar · Local). The "may fail when you switch back" warning shows for valar and local.
  - Yard: three paths (`pathA` top, `pathV` middle, `pathL` bottom) with `pctV`/`modelV`. A track with `configured: false` is drawn dashed with "not configured".
  - Tracks panel `id="tracks"`, under the yard: for valar and local, show the url, key status, and "Sonnet & bigger"/"Haiku" inputs with `<datalist>` from `/ui/api/models?track=…`. On fetch failure: "Couldn't load models (<error>). Type a model id." Save on change through the existing config PUT; on a 400, show the server's message.
  - Rulebook:
    - "Otherwise" gets a third button;
    - label track select gets 3 options;
    - the classifier card gets an upstream select (Valar / LiteLLM);
    - built-in descriptions for tags/tier match the new defaults;
    - the editor's `TOK` highlights `valar`;
    - `risky()` treats `go valar` like `go local`.
  - Departures: blue dot for valar; reason text `Valar not configured · 502` / `Valar down · 502`.
  - Side by side: replace the two checkboxes with three rows, "When <track> answers, compare with [select]" bound to `doubling.shadow`. Cards and verdict buttons use the pair's two tracks; the tally shows all tracks.

- [ ] **Step 4: Verify.**
  - Run `python3 -m unittest` → OK.
  - Start the router with `CLAUDE_ROUTER_CONFIG_DIR` set to a scratch dir and the repo `.env` loaded.
  - Open `http://127.0.0.1:8787/ui` in the browser (Playwright) and check:
    - no console errors;
    - 4 lever positions;
    - the Tracks panel lists Valar models;
    - changing the Valar haiku model persists across reload;
    - doubling selects persist;
    - local shows "no key".
  - Screenshot for the ledger.

- [ ] **Step 5: Commit** `git commit -am "feat: page shows and edits the valar track"`

---

### Task 6: Docs and live acceptance

**Files:**
- Modify: `README.md`, `env.example`

- [ ] **Step 1: Update docs.**
  - `env.example`: add `VALAR_URL`/`VALAR_API_KEY`; mark `LITELLM_API_KEY` optional; drop the retired model vars.
  - README:
    - the three tracks and the default rulebook table;
    - the Tracks panel;
    - retired env vars, and that models now live on the page;
    - the main-session limitation now covers Valar;
    - ValarCode rejected (one line);
    - the classifier sends prompt text to Valar by default.

- [ ] **Step 2: Live acceptance** (real Valar, key from the repo `.env`, scratch config dir; don't touch `~/.claude/settings.json`). Start the router; for each case run `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "<prompt>"` and read the log line:

  | Prompt | Expected log line |
  |---|---|
  | `hi` | `route=anthropic` |
  | `#execution say hi` | `route=valar model=…→zai-org/GLM-5.3 status=200` |
  | `use a general-purpose subagent with model haiku to say hi` | haiku lines `route=valar …→deepseek-ai/DeepSeek-V4.1-Flash-1017` |
  | `#local hi` | `status=502`, LiteLLM unreachable, no other route |

  Also: a sonnet subagent prompt with the classifier on shows `about:<label>` or a classifier note within the time limit. Record the results in the README acceptance section.

- [ ] **Step 3: Run all tests.** `python3 -m unittest` → OK.

- [ ] **Step 4: Commit** `git commit -am "docs: valar track; live acceptance"`
