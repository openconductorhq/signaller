# Cache stats and 1-hour cache-write pricing

## Why

The router already reads cache tokens from every response (`doubling.py`
`_usage`), stores them (`requests.cache_read_tok`, `cache_write_tok`) and prices
them. Two problems:

1. **You can't see cache use.** About 96–98% of input tokens come from cache, but
   the console never shows it. That share is the main reason the costs are low.
2. **1-hour cache writes are underpriced.** Anthropic charges 1.25× input for
   5-minute writes and 2× for 1-hour writes. The router prices every write at
   1.25×, so the "would have cost on Anthropic" figure is too low when Claude Code
   writes 1-hour entries. The response says which kind: `usage.cache_creation`
   splits `cache_creation_input_tokens` into `ephemeral_5m_input_tokens` and
   `ephemeral_1h_input_tokens`.

**Checked and correct, so no change:** the cache-read defaults in `prices.py`. On
Fable 5.1 a read is 0.025× input ($0.25 against $10) and on Opus 5.5 it is 0.05×
($0.20 against $4). Sonnet and Haiku are 0.1×.

**Also checked and correct (2026-11-04): Valar's per-window pricing.**
- Valar's live `/v1/models` lists the tiers `realtime`, `priority`, `standard`,
  `flex` and `batch`.
- `prices.VALAR_TIERS` maps realtime to `asap`, and priority and standard to
  themselves. `flex` and `batch` are never sent.
- Each row is priced at the listed rate for the window the router sent in
  `X-Valar-Completion-Window`.
- Recomputing the last 3,000 priced Valar rows gave 0 mismatches.
- The 1,151 Valar rows with no window are from 2026-10-28–29, before usage was
  recorded. They have no tokens, so no cost is expected.
- Valar lists no cache-write price, so its writes stay free.

## Goals

- Every response's 1-hour write tokens are recorded and priced at the 1-hour rate.
- **The cost cards** (Anthropic, and one per downstream) show how much of the
  input came from cache.
- **An opened request** shows its uncached, cache-read and cache-write tokens, and
  how many of the writes were 1-hour.

## Non-goals

- No backfill. Old rows have no 5-minute/1-hour split and keep being priced as
  5-minute writes, the same as today.
- No cache figures on the departure rows, the pills or the turn view cards.
- No change to how Valar is priced. Its cache writes are free, and its reads use
  its own listed `cached_input` price.
- No new prices for generic downstreams. A generic downstream's 1-hour writes are
  priced at its single `cache_write` price, since we don't know whether it bills
  them differently. Mark this in the code with a `ponytail:` comment.
- Haiku's long-prompt rate stays ignored (there is already a `ponytail:` note).

## Anti-goals

- **A pricing error must never fail a request.** This keeps the existing `try`
  around the cost calculation in `signaller.py`.
- **Old `prices.json` files must load as before.** A file without the new key
  gets the default for it.
- The cost cards get one short addition, not a new panel.

## Design

### Recording

- **Parsing.** `doubling.py` `_usage` also reads
  `u["cache_creation"]["ephemeral_1h_input_tokens"]` when it is an int and stores
  it as `usage["cache_write_1h"]`. It follows the same "later events win" rule as
  the other counts. When the field is absent, `cache_write_1h` stays absent (None).
- **Storage.** The `requests` table gets a new INTEGER column, `cache_write_1h_tok`.
  It's added to the fresh-database `CREATE TABLE`, to `COLS` and to the `_migrate`
  types map. `cache_write_tok` stays
  the total of all writes. `cache_write_1h_tok` is the 1-hour part of it: None
  means unknown, which is treated as 0.
- **Writing the row.** `signaller.py` writes `cache_write_1h_tok` from
  `usage.get("cache_write_1h")`.

### Pricing (`prices.py`)

- **Default sheet.** `DEFAULT_ANTHROPIC` gets a `cache_write_1h` key at 2× input:
  fable 20.0, opus 8.0, sonnet 4.0, haiku 0.20, other the same as sonnet. Update
  the comment above it so it covers both write rates.
- **Old files.** `load()` already merges each tier over the defaults, so an old
  `prices.json` gets the new key. Nothing is rewritten on disk.
- **`api_cost`.** The 1-hour writes (`min(cache_write_1h, cache_write)`) are
  priced at `cache_write_1h`, and the rest of `cache_write` at `cache_write`. With
  no split, or a split of 0, the result is exactly today's.
- **`cost` for a generic downstream.** Unchanged: all of `cache_write` at its
  `cache_write` price.
- **Valar.** Unchanged.
- **Downstream prices.** `KEYS` and `routing.PRICE_KEYS` stay at four keys. The
  new key is only on the Anthropic sheet. `api_cost` iterates over explicit keys,
  not `zip(KEYS)`.
- **`reprice`.** Unaffected. It only fills downstream `cost`, which doesn't use
  the split.

### API

- **Summary.** In `store.summary`, each track also gets `cache_read` and
  `cache_write`: the sums of `cache_read_tok` and `cache_write_tok` over the span
  and sessions, with COALESCE to 0. `NullStore.summary` gives the same keys as 0 on
  every track.
- **Requests.** `GET /ui/api/requests/<id>` already returns `SELECT *`, so the new
  column comes back with no change.

### Console

- **The cost cards.** The token line on each card (Anthropic and each downstream)
  gets a cached share after the in/out figures:
  `0.6M in / 1.4M out · 98% cached`.
  - cached % = `cache_read / (in + cache_read + cache_write)`, rounded to a whole
    percent.
  - It's left out when that sum is 0.
  - The line keeps its other parts ("partly unpriced", "incl. … comparisons").
  - The line's `title` (a tooltip, which the CSP allows) gives the full split:
    `X uncached · Y read from cache · Z written to cache`.
- **An opened request.** On the second facts line, the tokens part becomes
  `1.2k in / 300 out · 95k cache read · 2k cache write (1.5k 1h)`.
  - The cache parts appear only when they are above 0.
  - The `(… 1h)` part appears only when `cache_write_1h_tok` is above 0.
  - Use `tok()` for every number.

## Tests

One test for each piece of logic:

- `test_doubling`: a `message_start` whose usage has
  `cache_creation: {ephemeral_5m_input_tokens: 2, ephemeral_1h_input_tokens: 3}`
  gives `cache_write_1h == 3`. Without the object, it's absent or None.
- `test_prices`: `api_cost` on opus with 1M cache writes, of which 1M are 1-hour,
  is $8. With no split it is $5, which is today's figure.
- `test_prices`: an old `prices.json` without `cache_write_1h` loads, and opus gets
  8.0.
- `test_store`: an old database without the column gets it on open, and `summary`
  returns `cache_read` and `cache_write` per track.
- `test_proxy`: a proxied response with the 1-hour split writes
  `cache_write_1h_tok` to the row.
- **UI:** checked in a browser against a scratch router with seeded rows, as
  AGENTS.md says to.

## Left to the builder

- Exact wording and CSS of the cached share, within one short addition to the
  line.
- Whether the tooltip goes on the whole token line or on the "% cached" part.
