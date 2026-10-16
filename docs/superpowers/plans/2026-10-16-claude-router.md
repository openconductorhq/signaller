# claude-router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A loopback HTTP router that sits at `ANTHROPIC_BASE_URL` and sends each Claude Code request either to Anthropic (untouched) or to local LiteLLM (credentials swapped, model renamed), by prompt tag then model tier.

**Architecture:** One module `claude_router.py`: pure routing functions at the top, a `ThreadingHTTPServer` handler that reads the body, calls `decide()`, and streams the upstream response back chunk by chunk via `http.client`. Config from env vars only. Runs as a systemd user service.

**Tech Stack:** Python 3.12, standard library only (`http.server`, `http.client`, `json`, `re`, `unittest`). No third-party packages.

**Spec:** `docs/superpowers/specs/2026-10-16-claude-router-design.md`

## Global Constraints

- Standard library only; no `pip install`.
- Listen address must be loopback; default `127.0.0.1:8787`; refuse to start otherwise.
- `LITELLM_API_KEY` required; refuse to start if unset.
- Env vars and defaults: `CLAUDE_ROUTER_LISTEN=127.0.0.1:8787`, `CLAUDE_ROUTER_ANTHROPIC_URL=https://api.anthropic.com`, `LITELLM_URL=http://127.0.0.1:4000`, `CLAUDE_ROUTER_LOCAL_SONNET=claude-sonnet-4-5-20250929`, `CLAUDE_ROUTER_LOCAL_HAIKU=claude-haiku-4-5-20251001`.
- Anthropic route: no body rewriting; path, query, headers unchanged except hop-by-hop.
- Local route: never forward client `Authorization` / `x-api-key`; drop the `oauth-2025-04-20` beta.
- No fallback between routes in either direction.
- Never log bodies, prompts, or credential values.
- Tests: `python3 -m unittest -v` from repo root.

## Review Focus

1. **Client disconnects mid-stream** (user presses Esc): the handler must swallow `BrokenPipeError`/`ConnectionResetError`, close the upstream, log, and keep serving. Test in Task 2.
2. **Upstream non-2xx** (429, 529 overloaded, 401): status, headers and body pass through verbatim; no retry, no fallback. Test in Task 2.
3. **Message `content` as a plain string** (`"content": "fix it #local"`) rather than a block list: the tag still counts. Test in Task 1.
4. **Large bodies** (long histories, base64 images, several MB): forwarded byte-identical. Test in Task 2.
5. **Concurrent requests** (main session plus parallel subagents): two simultaneous streams both complete. Test in Task 2.

---

## File Structure

- `claude_router.py`: everything (routing functions, handler, config, `main()`).
- `tests/test_route.py`: unit tests for the pure routing functions.
- `tests/test_proxy.py`: integration tests with fake upstream servers on loopback.
- `tests/__init__.py`: empty.
- `claude-router.service`: systemd user unit.
- `env.example`: template for `~/.config/claude-router/env`.
- `README.md`: install, enable, bypass, limitations, manual acceptance.

---

### Task 1: Routing decision

**Files:**
- Create: `claude_router.py`, `tests/__init__.py`, `tests/test_route.py`

**Interfaces:**
- Produces:
  - `latest_human_text(messages: list) -> str`
  - `decide(method: str, path: str, body: bytes) -> tuple[str, str]`: returns `(route, reason)` with route ∈ `{"anthropic", "local"}` and reason ∈ `{"tag:plan", "tag:local", "tier", "passthrough"}`.
  - `local_model(model: str, sonnet: str, haiku: str) -> str`
  - `ROUTED_PATHS = ("/v1/messages", "/v1/messages/count_tokens")`, matched on the path without its query string.

- [ ] **Step 1: Write the failing tests** in `tests/test_route.py` (`unittest.TestCase`). Use a helper `body(messages, model="claude-opus-5-5") -> bytes` that JSON-encodes `{"model":..., "messages":...}`. Cases:
  - `test_tag_local_in_latest_human` → `decide("POST","/v1/messages?beta=true", body([user("do it #local")]))` == `("local","tag:local")`
  - `test_tag_plan_beats_haiku` → `#plan` on model `claude-haiku-4-5` == `("anthropic","tag:plan")`
  - `test_tag_local_beats_opus` → `#local` on opus == `("local","tag:local")`
  - `test_both_tags_anthropic` → `"#local and #plan"` == `("anthropic","tag:plan")`
  - `test_tag_case_insensitive_whole_word` → `"#LOCAL"` routes local; `"#localhost"` does not count as a tag (opus → `("anthropic","tier")`)
  - `test_tag_in_older_message_ignored` → messages `[user("#local"), assistant("ok"), user("next")]` on opus == `("anthropic","tier")`
  - `test_tool_result_only_message_skipped` → `[user("#local go"), assistant(tool_use), user([{"type":"tool_result",...}])]` == `("local","tag:local")`
  - `test_system_reminder_block_ignored` → last user msg blocks `[{"type":"text","text":"<system-reminder>#local</system-reminder>"},{"type":"text","text":"hello"}]` on opus == `("anthropic","tier")`
  - `test_string_content` → `{"role":"user","content":"fix #local"}` == `("local","tag:local")`
  - `test_tier` → opus `("anthropic","tier")`, `claude-sonnet-5-5` `("local","tier")`, `claude-haiku-5-5` `("local","tier")`, `"something-else"` `("anthropic","tier")`
  - `test_count_tokens_routed` → path `/v1/messages/count_tokens` with sonnet == `("local","tier")`
  - `test_passthrough` → `("GET","/api/hello",b"")`, `("POST","/api/oauth/x",b"{}")`, and `("POST","/v1/messages",b"not json")` all == `("anthropic","passthrough")`
  - `test_local_model` → `local_model("claude-haiku-5-5","S","H")=="H"`; `local_model("claude-opus-5-5","S","H")=="S"`; `local_model("claude-sonnet-5-5","S","H")=="S"`

- [ ] **Step 2: Run, expect failure**
  Run: `python3 -m unittest tests.test_route -v`
  Expected: ImportError / failures.

- [ ] **Step 3: Implement** the three functions plus `ROUTED_PATHS` in `claude_router.py`. Tag regex: `(?<!\w)#(plan|local)(?!\w)` with `re.I`. A user message counts as "human" if it has at least one text block whose stripped text does not start with `<system-reminder>`; the human text is the join of those blocks. Tier: `"haiku" in model or "sonnet" in model` → local. Wrap JSON parsing so any parse or shape error gives `("anthropic","passthrough")`.

- [ ] **Step 4: Run, expect pass**
  Run: `python3 -m unittest tests.test_route -v`
  Expected: all OK.

- [ ] **Step 5: Commit**
  `git add claude_router.py tests/ && git commit -m "feat: routing decision (tag, then model tier)"`

---

### Task 2: Streaming proxy handler

**Files:**
- Modify: `claude_router.py`
- Create: `tests/test_proxy.py`

**Interfaces:**
- Consumes: `decide`, `local_model` from Task 1.
- Produces:
  - `@dataclass Config(listen_host: str, listen_port: int, anthropic_url: str, litellm_url: str, litellm_key: str, local_sonnet: str, local_haiku: str)`
  - `make_server(cfg: Config) -> http.server.ThreadingHTTPServer`: bound, not yet serving. `daemon_threads = True`.
  - `local_headers(headers: Iterable[tuple[str, str]], key: str) -> list[tuple[str, str]]`

- [ ] **Step 1: Write the failing tests** in `tests/test_proxy.py`. Spin up two fake upstreams (`ThreadingHTTPServer` on `127.0.0.1:0`). Each one records the received method, path, headers and body. For SSE requests it replies with 3 events, sleeping 0.3 s between them. Then start `make_server(Config(..., listen_port=0, anthropic_url=<fake A>, litellm_url=<fake L>, litellm_key="LK", local_sonnet="S", local_haiku="H"))` in a thread. Cases:
  - `test_anthropic_passthrough_untouched` → POST opus body with `Authorization: Bearer sk-ant-oat01-X`, `anthropic-beta: claude-code-20250219,oauth-2025-04-20` to `/v1/messages?beta=true`. Fake A receives the same path+query, byte-identical body, the same `Authorization` and `anthropic-beta` values. Fake L receives nothing.
  - `test_local_swaps_credentials_and_model` → sonnet body with the same headers plus `x-api-key: client`. Fake L receives `Authorization: Bearer LK` and no `x-api-key`. `anthropic-beta` == `claude-code-20250219` (oauth beta removed). JSON body `model == "S"`, other fields unchanged. Fake A receives nothing.
  - `test_streams_incrementally` → read the first SSE event from the router and assert it arrives in under 0.25 s, before the upstream has finished.
  - `test_upstream_error_passthrough` → fake A returns 529 with body `{"type":"error","error":{"type":"overloaded_error"}}`. The client sees 529 and an identical body.
  - `test_litellm_down_502` → `litellm_url` points at a closed port. Sonnet request → 502 with JSON `{"type":"error","error":{"type":"api_error","message": <contains "LiteLLM unreachable">}}`.
  - `test_client_disconnect_mid_stream` → open a raw socket, send the request, read the first chunk, close. A following normal request still succeeds.
  - `test_large_body` → 5 MB string in a message. Fake A receives a byte-identical body.
  - `test_concurrent_streams` → two streaming requests in parallel threads both receive all 3 events within 2 s total.
  - `test_local_headers` (unit) → drops `authorization`, `x-api-key`, `host`, `content-length`, `connection`, `transfer-encoding`, case-insensitively. Adds `Authorization: Bearer <key>`. Strips `oauth-2025-04-20` from a comma list and removes the header entirely when nothing is left.

- [ ] **Step 2: Run, expect failure**
  Run: `python3 -m unittest tests.test_proxy -v`

- [ ] **Step 3: Implement** `Config`, `local_headers`, `make_server` and the handler in `claude_router.py`:
  - Handle all methods (`do_GET`/`do_POST`/`do_HEAD`/etc. share one method).
  - Read the full body via `content-length`.
  - Call `decide(...)`. For local routes, rewrite the JSON `model` with `local_model` and re-serialize.
  - Open `http.client.HTTP(S)Connection` to the chosen base (based on scheme), forwarding the original path+query.
  - Write status and headers (minus hop-by-hop and `content-length` when streaming), then loop `resp.read1(65536)` → `wfile.write` + `flush`.
  - Keep `protocol_version = "HTTP/1.0"`, so closing the connection marks the end of the response. Add the comment `# ponytail: HTTP/1.0 close-delimited responses, no keep-alive; switch to chunked if connection churn ever matters`.
  - Catch `BrokenPipeError`/`ConnectionResetError` while writing: close the upstream and return.
  - On connection errors to LiteLLM, send the 502 JSON from the spec. On connection errors to Anthropic, send the same shape with the message `"claude-router: Anthropic unreachable at <url>"`.
  - Log one line to stderr per request, formatted `"%(asctime)s route=%s reason=%s model=%s[→%s] status=%d ms=%d stream=%s"`. Silence `log_message`.

- [ ] **Step 4: Run all tests, expect pass**
  Run: `python3 -m unittest -v`

- [ ] **Step 5: Commit**
  `git add -A && git commit -m "feat: streaming proxy with per-request routing"`

---

### Task 3: Startup, service, docs, live acceptance

**Files:**
- Modify: `claude_router.py` (add `load_config`, `main`)
- Modify: `tests/test_route.py` (config tests)
- Create: `claude-router.service`, `env.example`, `README.md`

**Interfaces:**
- Consumes: `Config`, `make_server` from Task 2.
- Produces: `load_config(env: Mapping[str, str]) -> Config`, which raises `ValueError` with a clear message; `main() -> None`, which prints the error and exits 2 on `ValueError`; `if __name__ == "__main__": main()`.

- [ ] **Step 1: Write the failing tests** in `tests/test_route.py`:
  - `test_config_defaults` → `load_config({"LITELLM_API_KEY":"k"})` gives host `127.0.0.1`, port `8787`, and the defaults from Global Constraints.
  - `test_config_requires_key` → `load_config({})` raises `ValueError`.
  - `test_config_rejects_non_loopback` → `CLAUDE_ROUTER_LISTEN=0.0.0.0:8787` and `=192.168.1.5:8787` raise `ValueError`; `localhost:9000` and `[::1]:9000` are accepted. Use `ipaddress.ip_address(...).is_loopback`, treating `localhost` as loopback.

- [ ] **Step 2: Run, expect failure**, then **implement** `load_config` and `main`. `main` logs `listening on <host:port>; anthropic=<url> litellm=<url>` and calls `serve_forever()`. **Run, expect pass**: `python3 -m unittest -v`.

- [ ] **Step 3: Write `claude-router.service`**
  - `[Service] ExecStart=/usr/bin/python3 -I %h/Development/claude-router/claude_router.py`
  - `EnvironmentFile=%h/.config/claude-router/env`
  - `Restart=on-failure`
  - `[Install] WantedBy=default.target`

  Write `env.example` with every env var, including `LITELLM_API_KEY=` left blank.

- [ ] **Step 4: Write `README.md`.** Cover, briefly:
  - What it does (the routing table from the spec).
  - Install:
    ```
    mkdir -p ~/.config/claude-router
    cp env.example ~/.config/claude-router/env
    chmod 600 ~/.config/claude-router/env
    ln -s $PWD/claude-router.service ~/.config/systemd/user/
    systemctl --user enable --now claude-router
    ```
  - Enable in Claude Code: `"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787"}` in `~/.claude/settings.json`.
  - Bypass: `ANTHROPIC_BASE_URL= claude`.
  - Router down: every session fails to connect; bypass as above.
  - The main-session limitation from the spec ("keep the main session on opus; `#local` in main only for throwaway conversations").
  - Logs: `journalctl --user -u claude-router -f`.
  - Manual acceptance steps 1–4 from the spec.

- [ ] **Step 5: Live acceptance against real LiteLLM.**
  - Get the key from `~/litellm/config.yaml` `master_key` into a scratch env file. Do not commit it, and do not edit `~/.claude/settings.json`.
  - Start the router in the foreground.
  - Run `ANTHROPIC_BASE_URL=http://127.0.0.1:8787 claude -p "<prompt>"` for each acceptance case:
    1. `hi` → `route=anthropic reason=tier`.
    2. `#local say hi` → `route=local reason=tag:local`, status 200.
    3. `use a general-purpose subagent with model haiku to say hi` → opus lines on anthropic, haiku lines on local.
  - If LiteLLM rejects the request:
    - Wrong auth header → switch `local_headers` to `x-api-key` and update its test.
    - Rejected betas → remove `anthropic-beta` on the local route.
    - Thinking params → prefer `litellm_settings: drop_params: true` in LiteLLM config. Report that to the user instead of editing it yourself, since it's outside this repo.

  Record the results and any adjustments in the README's acceptance section.

- [ ] **Step 6: Commit**
  `git add -A && git commit -m "feat: config, systemd unit, README; live acceptance"`

---

**Not in this plan (by spec):** editing `~/.claude/settings.json`, enabling the service, and the LiteLLM `0.0.0.0` bind. These are the user's to switch on after review.
