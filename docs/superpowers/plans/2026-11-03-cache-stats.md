# Cache Stats Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record and price 1-hour cache writes, and show cache use on the cost
cards and on an opened request.

**Architecture:**
- `AnswerCapture._usage` adds a `cache_write_1h` count. It's stored in a new
  `requests.cache_write_1h_tok` column and priced at a new `cache_write_1h` rate on
  the Anthropic sheet.
- `store.summary` adds per-track cache sums, and the console shows them.

**Tech Stack:**
- Python 3 standard library (sqlite3, unittest/pytest).
- Vanilla JS and CSS under a strict CSP.

**Spec:** `docs/superpowers/specs/2026-11-03-cache-stats-design.md`

## Global Constraints

- **Dependencies:** standard library only; vanilla JS. No inline styles or scripts.
  A tooltip goes in a `title` attribute.
- **Never fail a request:** a pricing error must not fail one. Keep the existing
  `try` at `signaller.py:406`.
- **Old files load:** an old `prices.json` and an old `stats.db` load without
  errors. Nothing is backfilled.
- **Default 1-hour write prices (USD per 1M):** fable 20.0, opus 8.0, sonnet 4.0,
  haiku 0.20, other the same as sonnet.
- **Keys stay at four:** `prices.KEYS` and `routing.PRICE_KEYS` keep their four
  keys. Downstream pricing (`Prices.cost`, `reprice`) is unchanged.
- **Testing:** on a scratch router on port 8788 only, never on the live 8787, and
  never open the live `stats.db` (AGENTS.md).
- **Full suite:** `pytest -q tests` stays green, and `node --check ui/app.js`
  passes.

## Review Focus

1. **More 1-hour writes than writes.** If a model reports
   `ephemeral_1h_input_tokens` above `cache_creation_input_tokens`, the 1-hour
   part is clamped to the total. The cost is then never above `cache_write × 2×`.
   (Task 1, `test_api_cost_1h_clamped`.)
2. **The split arrives in `message_start` only.** The later `message_delta` has no
   `cache_creation`, and the 1-hour count survives. (Task 1, in
   `test_usage_1h_split`.)
3. **A `prices.json` that overrides only `in` for opus.** `cache_write_1h` still
   comes from the defaults (8.0) and isn't dropped. (Task 1,
   `test_old_prices_json_gets_1h`.)
4. **No stats store (`NullStore`).** The summary still has `cache_read`/`cache_write`
   as 0, so the cards render. (Task 2, in `test_nullstore_summary_cache_keys`.)
5. **A track with no tokens.** The card shows no "% cached" and no NaN. (Task 3,
   browser check.)

---

### Task 1: Record and price 1-hour cache writes

**Files:**
- Modify: `doubling.py` (`_usage`, about lines 57–64)
- Modify: `store.py` (`SCHEMA` line 24, `COLS` line 34, `_migrate` types line 191)
- Modify: `signaller.py:420` (row write)
- Modify: `prices.py` (`DEFAULT_ANTHROPIC` lines 9–18, `api_cost` lines 78–82)
- Test: `tests/test_doubling.py`, `tests/test_prices.py`, `tests/test_store.py`,
  `tests/test_proxy.py`

**Interfaces:**
- Produces:
  - `usage["cache_write_1h"]`, an int, present only when the response had
    `usage.cache_creation.ephemeral_1h_input_tokens` as an int.
  - The `requests.cache_write_1h_tok` INTEGER column, nullable.
  - `prices.DEFAULT_ANTHROPIC[tier]["cache_write_1h"]`.
  - `Prices.api_cost(model_in, usage)`, which reads `usage.get("cache_write_1h")`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_doubling.py` (class with `test_usage_sse_and_json`):
```python
def test_usage_1h_split(self):
    c = AnswerCapture("text/event-stream")
    u = {"input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 5,
         "cache_creation": {"ephemeral_5m_input_tokens": 2, "ephemeral_1h_input_tokens": 3}, "output_tokens": 1}
    c.feed(sse({"type": "message_start", "message": {"usage": u}},
               {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 7}}))
    self.assertEqual(c.result()["usage"], {"in": 10, "out": 7, "cache_read": 5, "cache_write": 5, "cache_write_1h": 3})
```
The existing usage tests must still pass unchanged: no `cache_write_1h` key
appears when the response has no split.

In `tests/test_prices.py` (`PricesTest`, using its `M = 1e6` and `self.p`):
```python
def test_api_cost_1h(self):
    self.assertAlmostEqual(self.p.api_cost("claude-opus-5-5", {"cache_write": M, "cache_write_1h": M}), 8.0)
    self.assertAlmostEqual(self.p.api_cost("claude-opus-5-5", {"cache_write": M}), 5.0)
    self.assertAlmostEqual(self.p.api_cost("claude-opus-5-5", {"cache_write": M, "cache_write_1h": M / 2}), 6.5)

def test_api_cost_1h_clamped(self):
    self.assertAlmostEqual(self.p.api_cost("claude-opus-5-5", {"cache_write": M, "cache_write_1h": 3 * M}), 8.0)

def test_old_prices_json_gets_1h(self):
    # write prices.json with {"opus": {"in": 4.0, "out": 20.0, "cache_read": 0.2, "cache_write": 5.0}} only, the way test_prices_json_written_and_honoured does
    # then Prices(dir).anthropic["opus"]["cache_write_1h"] == 8.0
    # and anthropic["fable"]["cache_write_1h"] == 20.0
```

In `tests/test_store.py`, extend `test_old_db_gets_columns`: after reopening,
`"cache_write_1h_tok"` is in `PRAGMA table_info(requests)`, and
`s.request(1)["cache_write_1h_tok"] is None`.

In `tests/test_proxy.py`:
- Change the `USAGE` fake upstream's `message_start` usage (line 85) to add
  `"cache_creation":{"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":2}`.
- In the test at about line 351, assert `ok[0]["cache_write_1h_tok"] == 2`.

- [ ] **Step 2: Run them to make sure they fail**

Run: `pytest -q tests/test_doubling.py tests/test_prices.py tests/test_store.py tests/test_proxy.py`
Expected: the new assertions FAIL, with a KeyError or a mismatch on
`cache_write_1h` or `cache_write_1h_tok`.

- [ ] **Step 3: Implement**

- **`doubling.py` `_usage`:** after the loop, read the 1-hour count as follows,
  so a later event without the object never erases an earlier count.
  ```python
  cc = u.get("cache_creation")
  if isinstance(cc, dict) and isinstance(cc.get("ephemeral_1h_input_tokens"), int):
      self.usage["cache_write_1h"] = cc["ephemeral_1h_input_tokens"]
  ```
- **`store.py`:** add `cache_write_1h_tok INTEGER` after `cache_write_tok` in
  `SCHEMA` and in `COLS["requests"]`, and `"cache_write_1h_tok": "INTEGER"` in the
  `_migrate` types map.
- **`signaller.py`:** add `"cache_write_1h_tok": usage.get("cache_write_1h")`
  to the `record_request` dict.
- **`prices.py`:**
  - Add `cache_write_1h` to each `DEFAULT_ANTHROPIC` row (the values are in Global
    Constraints; `_SONNET` gets 4.0, so `other` inherits it).
  - Change the comment to
    `# Cache write = 1.25 x input (5-minute entries), 2 x input (1-hour entries).`
  - Rewrite `api_cost` with explicit keys. Keep the `return None` when all four
    `KEYS` are None. Then:
    ```python
    w = usage.get("cache_write") or 0
    h = min(usage.get("cache_write_1h") or 0, w)
    return ((usage.get("in") or 0) * row["in"] + (usage.get("out") or 0) * row["out"]
            + (usage.get("cache_read") or 0) * row["cache_read"]
            + (w - h) * row["cache_write"] + h * row.get("cache_write_1h", 2 * row["in"])) / 1e6
    ```
  - Put `# ponytail: generic downstreams price 1h writes at their one cache_write rate`
    above `Prices.cost`'s generic return line.

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `pytest -q tests`
Expected: all pass (about 233).

- [ ] **Step 5: Commit**

```bash
git add doubling.py store.py signaller.py prices.py tests/
git commit -m "feat: record 1-hour cache writes and price them at 2x input on Anthropic"
```

### Task 2: Cache sums in the summary

**Files:**
- Modify: `store.py` (`summary` lines 368–385, `NullStore.summary`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `requests.cache_read_tok` and `cache_write_tok`, which already exist.
- Produces: `summary(...)["tracks"][t]["cache_read"]` and `["cache_write"]` as
  ints, 0 when there are none, for every track including removed ones. The
  `NullStore` summary has the same keys.

- [ ] **Step 1: Write the failing tests**

In `test_summary`, after the existing records, assert that each track's
`cache_read`/`cache_write` equals the sum of what that test recorded. If the
existing rows have no cache counts, add a row with `cache_read_tok=100,
cache_write_tok=10` on `valar` and assert `tracks["valar"]["cache_read"] == 100`
and `["cache_write"] == 10`, and `tracks["anthropic"]["cache_read"] == 0`.

Add `test_nullstore_summary_cache_keys`. For every track in
`NullStore().summary(...)["tracks"]` (call it the way the console does; see
`console.py`), `cache_read == 0` and `cache_write == 0`. If `NullStore.summary`
returns no tracks, assert on what it does return, and make the console's card
code tolerate missing keys (`|| 0`) in Task 3.

- [ ] **Step 2: Run them to make sure they fail**

Run: `pytest -q tests/test_store.py -k "summary"`
Expected: FAIL with a KeyError on `cache_read`.

- [ ] **Step 3: Implement**

- **The `summary` SQL:** add
  `COALESCE(SUM(cache_read_tok),0) cr, COALESCE(SUM(cache_write_tok),0) cw`.
- **The defaults:** add `"cr": 0, "cw": 0` to the `z` dict.
- **The output:** add `"cache_read": r["cr"], "cache_write": r["cw"]` to each
  `out_tracks[t]`.
- **`NullStore`:** mirror the keys.

- [ ] **Step 4: Run the tests to make sure they pass**

Run: `pytest -q tests`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add store.py tests/test_store.py
git commit -m "feat: summary carries per-track cache read/write totals"
```

### Task 3: Show cache use in the console

**Files:**
- Modify: `ui/app.js` (`renderMoney` lines 236–248, `explain` lines 852–856)
- Modify: `ui/app.css` only if the new text needs a style

**Interfaces:**
- Consumes: `tracks[t].cache_read` and `.cache_write` from Task 2, and
  `d.cache_write_1h_tok` from Task 1 (the request API returns `SELECT *`).

- [ ] **Step 1: The cost cards**

In `renderMoney`, add one helper next to `io`:
- `cached(x)`: `s = x.in + (x.cache_read||0) + (x.cache_write||0)`. It returns
  `s ? Math.round(100 * (x.cache_read||0) / s) + "% cached" : ""`.
- `split(x)`: returns
  `${tok(x.in)} uncached · ${tok(x.cache_read||0)} read from cache · ${tok(x.cache_write||0)} written to cache`.

The changes:
- **Downstream cards:** add `cached(v)` to the `.t` line's parts (after `io(v)`,
  dropped when empty by the existing `filter(Boolean)`), and set
  `title: split(v)` on that `.t` div.
- **Anthropic card:** set `$("mA").textContent` to `[io(t.anthropic),
  cached(t.anthropic)].filter(Boolean).join(" · ")`, and set `$("mA").title`
  to `split(t.anthropic)`.

- [ ] **Step 2: An opened request**

In `explain`, replace the tokens part of the second facts line with:
```js
d.in_tok != null && [`${tok(d.in_tok)} in / ${tok(d.out_tok)} out`,
  d.cache_read_tok > 0 && `${tok(d.cache_read_tok)} cache read`,
  d.cache_write_tok > 0 && `${tok(d.cache_write_tok)} cache write` + (d.cache_write_1h_tok > 0 ? ` (${tok(d.cache_write_1h_tok)} 1h)` : "")
].filter(Boolean).join(" · ")
```

- [ ] **Step 3: Parse check**

Run: `node --check ui/app.js && pytest -q tests/test_console.py`
Expected: no output from node, and the tests pass. That includes
`test_page_assets_local_only`.

- [ ] **Step 4: Browser check on a scratch router**

1. Seed `<scratchpad>/r8788/stats.db` with `store.Store(...)` in a short script
   with these rows:
   - an Anthropic row with `in_tok=600, cache_read_tok=59400, cache_write_tok=1000, cache_write_1h_tok=800`
   - a valar row with cache tokens
   - a valar row with all tokens 0
2. Start the scratch router:
   `SIGNALLER_CONFIG_DIR=<scratchpad>/r8788 SIGNALLER_LISTEN=127.0.0.1:8788 python3 signaller.py`
3. Open `http://127.0.0.1:8788/ui`.
4. Check:
   - the Anthropic card says `… · 97% cached` and its tooltip shows the split
   - a track with no tokens shows no "% cached"
   - opening the Anthropic request shows `59.4k cache read · 1k cache write (800 1h)`, in whatever format `tok` uses
   - the console has no errors
5. Stop the scratch router.

- [ ] **Step 5: Commit**

```bash
git add ui/app.js ui/app.css
git commit -m "feat(ui): cost cards show the cached share; an opened request shows its cache reads and writes"
```
