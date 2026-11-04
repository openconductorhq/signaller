"use strict";
// claude-router cockpit: polls the JSON API once a second and edits the routing config as a whole.
const $ = (id) => document.getElementById(id);
// Tracks: Anthropic (fixed) then the config's downstreams; a track id seen in history but no longer configured is "removed".
const DS = () => (cfg ? cfg.downstreams : []);
const dsOf = (t) => DS().find((d) => d.id === t);
const TRACKS = () => ["anthropic", ...DS().map((d) => d.id)];
const gone = (t) => t !== "anthropic" && !dsOf(t);
const NAME = (t) => t === "anthropic" ? "Anthropic" : dsOf(t) ? dsOf(t).name : t;
const label = (t) => NAME(t) + (gone(t) ? ", removed" : "");
// Each track's colour reaches CSS as --c on its elements.
const col = (t) => t === "anthropic" ? "var(--anth)" : dsOf(t) ? dsOf(t).color : "var(--off)";
const cv = (t) => "--c:" + col(t);
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

const SVG = new Set(["circle", "path", "text", "tspan"]);
function el(tag, attrs, ...kids) {
  const e = SVG.has(tag) ? document.createElementNS($("yard").namespaceURI, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "class") e.setAttribute("class", v);
    else if (k === "style") { const [p, x] = v.split(/:(.*)/); e.style.setProperty(p, x); }  // CSSOM: the page's CSP blocks style attributes
    else e.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of kids.flat()) if (c != null && c !== false) e.append(c instanceof Node ? c : String(c));
  return e;
}
const trk = (t) => el("span", { class: "to", style: cv(t) }, label(t));

async function api(method, path, body, ms = 5000) {
  const opt = { method, headers: {}, signal: AbortSignal.timeout(ms) };  // a stalled fetch must not stop polling
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch("/ui/api/" + path, opt);
  let j = null;
  try { j = await r.json(); } catch (_) { /* non-JSON error */ }
  if (!r.ok) { const e = new Error((j && j.error) || "HTTP " + r.status); e.status = r.status; e.body = j; throw e; }
  return j;
}

const pad = (n) => String(n).padStart(2, "0");
const hms = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
const fmtMs = (ms) => ms == null ? "–" : ms < 1000 ? ms + " ms" : ms < 10000 ? (ms / 1000).toFixed(1) + " s" : Math.round(ms / 1000) + " s";
const fmtUp = (s) => s < 60 ? s + "s" : s < 3600 ? Math.floor(s / 60) + "m" : s < 86400 ? `${Math.floor(s / 3600)}h ${Math.floor(s / 60) % 60}m` : `${Math.floor(s / 86400)}d ${Math.floor(s / 3600) % 24}h`;
const isErr = (status) => status === 0 || status >= 500;
const tierOf = (m) => { m = (m || "").toLowerCase(); return ["haiku", "sonnet", "opus", "fable"].find((t) => m.includes(t)) || "other"; };  // as routing.tier_of

const hm = (ts) => { const d = new Date(ts * 1000); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const usd = (v) => "$" + (!v ? "0" : v < 0.1 ? v.toFixed(3) : v.toFixed(2));
const tok = (n) => !n ? "0" : n >= 1e5 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "k" : n >= 1e3 ? (n / 1e3).toFixed(1) + "k" : String(n);
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
// What a request cost, for a departure or a side-by-side card; null when there's nothing to say.
function priced(track, cost, api, named) {  // named: say which downstream (where nothing else on screen does)
  if (track === "anthropic") return api != null ? usd(api) + " at API prices" : null;
  const on = named ? " on " + NAME(track) : "";
  if (cost == null) return "unpriced" + on;
  return usd(cost) + on + (api != null ? ` (would be ${usd(api)} on Anthropic)` : "");
}

function showErr(node, msg) { node.textContent = msg; node.hidden = !msg; }

// Re-rendering replaces nodes; put focus back on the control with the same data-k.
function keepFocus(fn) {
  const a = document.activeElement, k = a && a.dataset ? a.dataset.k : null;
  fn();
  if (k && document.activeElement !== a) {
    const e = document.querySelector(`[data-k="${CSS.escape(k)}"]`);
    if (e) e.focus({ preventScroll: true });
  }
}

/* ---------- what's shown: picked sessions + time span ---------- */
const SPANS = { "15m": "last 15 min", "1h": "last hour", "24h": "last 24 h", whole: "whole session", custom: null };
const VIEW_KEY = "claude-router.view";
let view = { pick: [], span: "1h", from: "", to: "" }, sessions = null, moreOpen = false, viewGen = 0;
try { Object.assign(view, JSON.parse(localStorage.getItem(VIEW_KEY)) || {}); } catch (_) { /* no storage: defaults */ }
if (!Array.isArray(view.pick) || !Object.hasOwn(SPANS, view.span)) view = { pick: [], span: "1h", from: "", to: "" };
view.pick = view.pick.filter((x) => typeof x === "string");
const saveView = () => { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (_) { /* no storage */ } };
const toTs = (v) => { const t = new Date(v).getTime(); return isNaN(t) ? null : t / 1000; };
const toLocal = (ts) => { const d = new Date(ts * 1000); return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16); };
const sessQ = () => view.pick.length ? "sessions=" + encodeURIComponent(view.pick.join(",")) : "";
function spanQ() {
  const now = Date.now() / 1000;
  if (view.span === "15m") return "from=" + (now - 900);
  if (view.span === "24h") return "from=" + (now - 86400);
  if (view.span === "whole") return "from=session";
  if (view.span === "custom") {
    const f = toTs(view.from), t = toTs(view.to);
    return [f != null && "from=" + f, t != null && "to=" + t].filter(Boolean).join("&");
  }
  return "";  // 1 hour: the server's default
}
const qs = (...parts) => { const q = parts.filter(Boolean).join("&"); return q ? "?" + q : ""; };
function spanText() {
  if (view.span !== "custom") return SPANS[view.span];
  const f = toTs(view.from), t = toTs(view.to);
  return `${f != null ? hm(f) : "start"}–${t != null ? hm(t) : "now"}`;
}
const shortLabel = (id) => {
  const s = sessions && sessions.find((x) => x.id === (id || "unknown"));
  return s ? s.label.split(" · ")[0] : id ? id.slice(0, 8) : "unknown session";
};
const fullLabel = (id) => { const s = sessions && sessions.find((x) => x.id === (id || "unknown")); return s ? s.label : id || "unknown session"; };

let sessSeq = 0;
// The chips: sessions with a request in the span, newest activity first ("Whole session" = all kept history).
async function loadSessions() {
  const seq = ++sessSeq;
  const list = await api("GET", "sessions" + qs(view.span === "whole" ? "from=0" : spanQ()));
  if (seq !== sessSeq) return;  // the span changed meanwhile
  sessions = list;
  const keep = view.pick.filter((id) => sessions.some((x) => x.id === id));  // picked sessions outside the span drop silently
  if (keep.length !== view.pick.length) { view.pick = keep; saveView(); viewChanged(true); }
  renderSess();
}
function renderSess() {
  const all = sessions || [], picked = new Set(view.pick);
  const shown = moreOpen ? all : all.filter((x, i) => i < 8 || picked.has(x.id));
  const chip = (attrs, ...kids) => el("button", { type: "button", class: "sc", ...attrs }, ...kids);
  keepFocus(() => $("sess").replaceChildren(el("span", { class: "lbl" }, "Showing"),
    chip({ class: "sc all", "aria-pressed": String(!picked.size), "data-k": "sc:all", onclick: () => setPick([]) }, `All ${plural(all.length, "session")}`),
    ...shown.map((x) => el("span", { class: "scw" }, chip({ "aria-pressed": String(picked.has(x.id)), "data-k": "sc:" + x.id, title: x.id,
      onclick: () => setPick(picked.has(x.id) ? view.pick.filter((y) => y !== x.id) : [...view.pick, x.id]) },
      x.label, el("small", {}, x.last_ts ? hm(x.last_ts) : "")),
      el("button", { type: "button", class: "scx", "data-k": "scx:" + x.id, title: "Delete this session's history",
        "aria-label": `Delete ${x.label}'s history`, onclick: () => forgetSession(x) }, "×"))),
    all.length > shown.length || moreOpen && all.length > 8 ? chip({ class: "sc more", "data-k": "sc:more", "aria-expanded": String(moreOpen),
      onclick: () => { moreOpen = !moreOpen; renderSess(); } }, moreOpen ? "fewer" : `+ ${all.length - shown.length} more`) : ""));
  // prompt and turn tags drawn before a session was named (or listed) catch up
  for (const t of document.querySelectorAll(".stag[data-sid]")) t.textContent = shortLabel(t.dataset.sid || null);
}
async function forgetSession(x) {
  if (!confirm(`Delete the history of ${x.label}? Its requests, prompts and pairs go. If it's still running, it starts again with no history.`)) return;
  try {
    await api("DELETE", "sessions/" + encodeURIComponent(x.id), undefined, 15000);
    if (view.pick.includes(x.id)) { view.pick = view.pick.filter((y) => y !== x.id); saveView(); }
    viewChanged(true);
    await loadSessions();
  } catch (e) {
    alert("Couldn't delete the session: " + e.message);
  }
}
function setPick(ids) { view.pick = ids; saveView(); renderSess(); if (cfg) renderLever(); viewChanged(true); }
// A new pick or span restarts departures (and side by side).
function viewChanged(pickToo) {
  viewGen++;
  if (pickToo) {
    $("deps").replaceChildren();
    lastId = 0; minId = null; firstLoad = true; $("older").hidden = true;
    turns = []; curTurn = null; turnData = null; turnKey = ""; ticks = 0;
  }
  busy = false;  // the reply in flight is for the old view; it's dropped by viewGen
  tick();
}
function renderSpan() {
  for (const b of $("span").children) { b.setAttribute("aria-pressed", String(b.dataset.v === view.span)); b.dataset.k = "span:" + b.dataset.v; }
  $("custom").hidden = view.span !== "custom";
  $("cFrom").value = view.from; $("cTo").value = view.to;
}
for (const b of $("span").children) b.addEventListener("click", () => {
  if (b.dataset.v === view.span) return;
  view.span = b.dataset.v;
  if (view.span === "custom" && !view.from) { const now = Date.now() / 1000; view.from = toLocal(now - 3600); view.to = toLocal(now); }
  saveView(); renderSpan(); viewChanged(true); loadSessions().catch(() => {});
});
for (const id of ["cFrom", "cTo"]) $(id).addEventListener("change", () => {
  view[id === "cFrom" ? "from" : "to"] = $(id).value; saveView(); viewChanged(true); loadSessions().catch(() => {});
});

/* ---------- state + polling ---------- */
let S = null, cfg = null, rendered = "", saving = 0, ticks = 0, busy = false;

async function tick() {
  if (busy || document.hidden) return;
  busy = true;
  const gen = viewGen;
  try {
    const [st, deps] = await Promise.all([api("GET", "state" + qs(sessQ(), spanQ())),
      api("GET", "departures" + qs(sessQ(), spanQ(), lastId && "since=" + Math.max(0, lastId - OVERLAP)))]);
    if (gen !== viewGen) return;
    $("offline").hidden = true;
    applyState(st);
    addDeps(deps);
    if (ticks++ % 5 === 0) {
      await loadTurns().catch((e) => showErr($("dblErr"), "Couldn't load doubled turns: " + e.message));
      if (ticks > 1) loadSessions().catch(() => {});  // ponytail: list refreshes every 5 s; new sessions show up then
    }
  } catch (e) {
    if (gen === viewGen) $("offline").hidden = false;
  } finally {
    if (gen === viewGen) busy = false;
  }
}

function applyState(st) {
  S = st;
  if (st.clock) $("sttClock").textContent = "Router time " + hm(st.clock.now) + " (" + st.clock.tz + ")";  // a wrong zone must be visible
  const n = view.pick.length;  // departures list the span's requests, paged back with "Load older"
  $("depFrom").textContent = `${plural(st.summary.total, "request")}${n ? ` from the ${n === 1 ? "picked session" : n + " picked sessions"}` : ""} · ${spanText()}`;
  const s = st.stats;
  $("banner").hidden = !st.banner;
  $("banner").textContent = st.banner || "";
  $("who").textContent = `${st.listen} · up ${fmtUp(st.uptime_s)}`;
  $("errs").hidden = !s.errors_last_hour;
  $("errs").textContent = `${s.errors_last_hour} error${s.errors_last_hour === 1 ? "" : "s"} in the last hour`;
  const m = st.summary, span = spanText();
  $("share").textContent = (view.span === "custom" ? "share of " : "share of the ") + span;
  $("cc").textContent = view.pick.length ? "Claude Code · " + plural(view.pick.length, "session") : "Claude Code";
  renderMoney(m, view.span === "whole" ? "" : " · " + span);
  renderCounts(m);
  const pm = m.per_minute;
  $("rpm").textContent = `${pm < 10 && pm % 1 ? pm.toFixed(1) : Math.round(pm)} req/min`;
  if (!view.pick.length) $("dblCount").textContent = `${s.doubled_turns_today} doubled turn${s.doubled_turns_today === 1 ? "" : "s"} today · you've judged ${s.judged_today}`;
  const t = $("tally");
  if (t) t.textContent = tallyText();
  // don't clobber a field the user is typing in, or an optimistic edit in flight
  const f = document.activeElement;
  const typing = f && f.matches("input:not([type=checkbox]):not([type=range]),select,textarea") && ($("rbBody").contains(f) || $("strip").contains(f) || $("tracks").contains(f) || $("stt").contains(f));
  if (!saving && !typing) { cfg = st.config; renderConfig(); }  // again after a save: fresh collisions, fresh settings
  if (cfg) { keepFocus(renderLever); renderYardNums(m, span); }  // a session's lever can change from /router at any time
}
// Downstream POST/DELETE/key PUT change the config on the server: reload it (even after an error or a timeout),
// so the next config PUT starts from it, and let the touched downstream's models load afresh.
async function dsCall(method, path, body, id) {
  let r, err = null;
  try { r = await api(method, path, body, 15000); } catch (e) { err = e; }  // POST probes the url first: up to 5 s
  ASKED.delete(id); delete MODELS[id]; delete WARN[id];
  try {
    const st = await api("GET", "state" + qs(sessQ(), spanQ()));
    applyState(st);
    cfg = st.config;
    renderConfig(true);
  } catch (e) { if (!err) throw e; }  // the call's own error is the one to show
  if (err) throw err;
  return r;
}

// Anthropic, one card per downstream (and per removed one with rows in the span), then the savings over all of them.
function renderMoney(m, sfx) {
  const t = m.tracks, io = (x) => `${tok(x.in)} in / ${tok(x.out)} out`, ds = Object.keys(t).filter((k) => k !== "anthropic"),
    cached = (x) => { const s = x.in + (x.cache_read || 0) + (x.cache_write || 0); return s ? Math.round(100 * (x.cache_read || 0) / s) + "% cached" : ""; },
    lat = (x) => x.latency ? `latency: ${fmtMs(x.latency.ttfb)} first byte · ${fmtMs(x.latency.ms)} total · ${x.latency.n} request${x.latency.n === 1 ? "" : "s"}` : "",
    latT = (x) => x.latency ? `median of ${x.latency.n} answered requests · mean ${fmtMs(Math.round(x.latency.ttfb_mean))} first byte / ${fmtMs(Math.round(x.latency.ms_mean))} total · errors excluded` : "",
    split = (x) => `${tok(x.in)} uncached · ${tok(x.cache_read || 0)} read from cache · ${tok(x.cache_write || 0)} written to cache`;
  $("money").style.setProperty("--cols", ds.length <= 3 ? `repeat(${ds.length + 1},minmax(0,1fr)) 1.3fr` : "repeat(4,minmax(0,1fr))");
  $("money").replaceChildren($("money").firstElementChild, ...ds.map((k) => { const v = t[k]; return el("div", { class: "mcell", style: cv(k) },
    el("div", { class: "k" }, label(k), el("span", { class: "sfx" })), el("div", { class: "n" }, usd(v.cost + v.comparisons)),
    el("div", { class: "t", title: split(v) }, [io(v), cached(v), v.unpriced > 0 && "partly unpriced", v.comparisons > 0 && `incl. ${usd(v.comparisons)} comparisons`].filter(Boolean).join(" · ")),
    v.latency && el("div", { class: "t", title: latT(v) }, lat(v))); }), $("mcS"));
  for (const e of document.querySelectorAll("#money .sfx")) e.textContent = sfx;
  $("nA").textContent = usd(t.anthropic.api_cost);
  $("mA").textContent = [io(t.anthropic), cached(t.anthropic)].filter(Boolean).join(" · ");
  $("mA").title = split(t.anthropic);
  $("lA").textContent = lat(t.anthropic);
  $("lA").title = latT(t.anthropic);
  $("nS").textContent = "≈ " + usd(m.savings);
  $("mS").textContent = `${ds.map(NAME).join(" + ") || "Downstream"} requests would have cost ${usd(m.saved_would)} on Anthropic`;
}
// "decided N of M" under each rule and Otherwise, for the picked sessions and span
function renderCounts(m) {
  m = m || (S && S.summary);
  if (!m) return;
  for (const e of document.querySelectorAll(".dec")) e.textContent = m.total ? `decided ${Object.hasOwn(m.rules, e.dataset.r) ? m.rules[e.dataset.r] : 0} of ${m.total}` : "";
}

/* ---------- config editing ---------- */
async function save(next, errNode) {
  const prev = cfg;
  cfg = next;
  renderConfig();
  saving++;
  try {
    cfg = await api("PUT", "config", next);
    showErr(errNode, "");
    renderConfig();
    return true;
  } catch (e) {
    cfg = prev;
    renderConfig(true);
    showErr(errNode, e.status === 400 ? e.message : "Couldn't save: " + e.message);
    return false;
  } finally {
    saving--;
  }
}
const withRules = (rules) => ({ ...cfg, rules });

function renderConfig(force) {
  const key = JSON.stringify(cfg);
  if (!cfg || (key === rendered && !force)) return;
  rendered = key;
  keepFocus(() => { renderYard(); renderLever(); renderTracks(); renderRules(); renderOtherwise(); renderSettings(); renderStrip(); });
}

/* lever: Anthropic, Rules, then one button per downstream */
const leverBtns = () => [...$("lever").querySelectorAll(".lever")];
// One picked session: the lever shows and sets that session's own lever (/router), falling back to the global one.
const oneSess = () => view.pick.length === 1 && view.pick[0] !== "unknown" ? view.pick[0] : null;
const ownLever = () => { const id = oneSess(); return id && S && S.session_levers ? S.session_levers[id] || null : null; };
const curLever = () => ownLever() || cfg.lever;
function renderLever() {
  const lv = curLever(), id = oneSess(), own = ownLever();
  for (const b of leverBtns()) {
    const on = b.dataset.v === lv;
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1;
  }
  const off = lv !== "rules";
  $("rulebook").classList.toggle("dim", off);
  $("leverNote").hidden = !off;
  $("leverNote").style.color = off ? col(lv) : "";
  $("leverNote").textContent = off ? `The lever is sending ${id ? "this session" : "everything"} to ${NAME(lv)}` : "";
  for (const [t, y] of Object.entries(YARD)) y.trk.classList.toggle("dim", off && t !== lv);
  $("jlabel").textContent = off ? "lever" : "rulebook";
  if (!offTrack(lv)) $("localWarn").hidden = true;
  const sc = $("leverScope");
  if (!id) sc.replaceChildren("Lever for ", el("b", {}, "all sessions"));
  else sc.replaceChildren("Lever for ", el("b", {}, fullLabel(id)), own ? " · its own" : ` · following the global lever (${cfg.lever === "rules" ? "Rules" : NAME(cfg.lever)})`,
    own ? " " : "", own ? el("button", { type: "button", class: "linkish", "data-k": "lever:reset", onclick: () => setLever(null) }, "back to global") : "");
}
const offTrack = (v) => !!dsOf(v);
function warnOff(v) {
  $("localWarnT").textContent = `Conversations that run on ${NAME(v)} may fail when you switch back. Start a new session afterwards if they do.`;
  $("localWarn").hidden = false;
}
async function setLever(v) {
  if (!cfg) return;
  const id = oneSess();
  if (id) {
    if (v === ownLever() || (v === null && !ownLever())) return;
    try {
      await api("PUT", "lever", { session: id, lever: v });
      const { [id]: _, ...rest } = S.session_levers || {};
      S.session_levers = v ? { ...rest, [id]: v } : rest;
      showErr($("leverErr"), "");
      renderLever();
      if (offTrack(v)) warnOff(v);
    } catch (e) {
      showErr($("leverErr"), "Couldn't set this session's lever: " + e.message);
    }
    return;
  }
  if (v === cfg.lever) return;
  if (await save({ ...cfg, lever: v }, $("leverErr")) && offTrack(v)) warnOff(v);
}
for (const b of leverBtns()) b.dataset.k = "lever:" + b.dataset.v;
const fixedLever = leverBtns();
function buildLever() {
  $("lever").replaceChildren(...fixedLever, ...DS().map((d) => el("button", { type: "button", class: "lever", role: "radio", "aria-checked": "false",
    "data-v": d.id, "data-t": d.id, "data-k": "lever:" + d.id, style: cv(d.id) }, el("b", {}, d.name), el("small", {}, "everything, ignore rules"))));
}
$("lever").addEventListener("click", (e) => { const b = e.target.closest(".lever"); if (b) setLever(b.dataset.v); });
$("lever").addEventListener("keydown", (e) => {
  const d = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key], bs = leverBtns(), i = bs.indexOf(e.target.closest(".lever"));
  if (!d || i < 0) return;
  e.preventDefault();
  const n = bs[(i + d + bs.length) % bs.length];
  n.focus();
  setLever(n.dataset.v);
});
$("localWarnX").addEventListener("click", () => { $("localWarn").hidden = true; });

/* the yard: Anthropic on top, one line per downstream below it; the first runs straight on (y=125) */
const YARD = {};  // track → its line, ghost path (for dots), share text and "no key" note
let dsKey = "";
function renderYard() {
  const key = JSON.stringify(DS().map((d) => [d.id, d.name, d.color]));
  if (key === dsKey) return;
  dsKey = key;
  buildLever();
  for (const k of Object.keys(YARD)) delete YARD[k];
  YARD.anthropic = { trk: $("trkA"), ghost: $("pathA"), pct: $("pctA") };
  const out = [];
  DS().forEach((d, i) => {
    const y = 125 + 85 * i, tail = i ? ` C520 125 540 ${y} 640 ${y} H960` : " H960", st = cv(d.id), model = el("tspan", { class: "model" });
    const L = YARD[d.id] = { trk: el("path", { class: "trk", "data-t": d.id, style: st, d: "M430 125" + tail }), ghost: el("path", { class: "ghost", d: "M30 125 H430" + tail }),
      pct: el("text", { x: 960, y: y - 14, class: "big", "data-t": d.id, style: st, "text-anchor": "end" }, "–"), model };
    out.push(L.trk, L.ghost, el("text", { x: 660, y: y - 14, class: "branch" }, d.name, model), L.pct);
  });
  $("lines").replaceChildren(...out);
  $("yard").setAttribute("viewBox", `0 0 1000 ${Math.max(165, 85 * DS().length + 80)}`);
  $("refIds").replaceChildren(...DS().flatMap((d, i) => [i ? ", " : "", el("code", {}, d.id)]));
}
function renderYardNums(m, span) {
  const tr = m.tracks, none = !m.total, pct = (t) => (tr[t] ? tr[t].pct : 0) + "%";
  for (const [t, L] of Object.entries(YARD)) {
    L.pct.textContent = none ? "–" : pct(t);
    if (!L.model) continue;
    const k = S.downstreams[t], off = !k || !k.key_set;
    L.model.textContent = off ? " · not configured: no key" : "";
    L.trk.classList.toggle("off", off);
    L.pct.classList.toggle("off", off);
  }
  $("yard").setAttribute("aria-label", none ? `Live traffic: no requests in the ${span}`
    : `Live traffic: ${TRACKS().map((t) => pct(t) + " " + NAME(t)).join(", ")} in the ${span}`);
}

/* tracks: one row per downstream — name, models per tier, Valar's windows, a generic one's prices, key, delete */
const TIER = { fable: "Fable", opus: "Opus", sonnet: "Sonnet", haiku: "Haiku" };
const PK = { in: "in", out: "out", cache_read: "cache read", cache_write: "cache write" };
const MAX_DS = 6;
const saveDs = (t, patch) => save({ ...cfg, downstreams: cfg.downstreams.map((d) => (d.id === t ? { ...d, ...patch } : d)) }, $("trErr"));
// Prices: all four filled (0 or more) or all four blank (unpriced); anything in between is refused.
function readPrices(inputs) {
  const v = inputs.map((x) => x.value.trim());
  if (v.every((x) => x === "")) return { prices: null };
  if (v.some((x) => x === "" || !(Number(x) >= 0))) return { error: "Fill in all four prices (0 or more), or leave all four blank for unpriced." };
  return { prices: Object.fromEntries(Object.keys(PK).map((k, i) => [k, Number(v[i])])) };
}
const priceInputs = (name, vals, attrs) => Object.entries(PK).map(([pk, pn]) => el("label", {}, pn + " ",
  el("input", { type: "number", min: 0, step: "any", value: vals ? vals[pk] : "", placeholder: "–", "aria-label": `${name} price for ${pn}, USD per 1M tokens`, ...attrs(pk) })));
const ROWERR = {};  // track → { msg, refs } from its last key/delete call
function renderTracks() {
  for (const d of DS()) if (!ASKED.has(d.id)) loadModels(d.id);
  $("trows").replaceChildren(...DS().map(trow));
  const full = DS().length >= MAX_DS;
  $("addDs").disabled = full;
  $("addDsWhy").hidden = !full;
}
function trow(d) {
  const t = d.id, k = S && S.downstreams[t], keyed = !!(k && k.key_set), valar = d.kind === "valar", re = ROWERR[t];
  const cur = () => dsOf(t);  // handlers read the config as it is when they fire, not as it was drawn
  const pErr = el("p", { class: "err", role: "alert", hidden: true }), keyIn = el("input", { type: "password", autocomplete: "off", placeholder: "Replace key",
    "data-k": "k:" + t, "aria-label": `New key for ${d.name} (empty keeps the current one)` });
  const act = async (fn) => {
    delete ROWERR[t];
    try { await fn(); } catch (e) { ROWERR[t] = { msg: e.message, refs: e.status === 409 && e.body && e.body.references }; renderConfig(true); }
  };
  return el("div", { class: "trow", "data-t": t, style: cv(t) },
    el("div", {}, el("input", { class: "tname", value: d.name, spellcheck: "false", "data-k": "n:" + t, "aria-label": `Name of ${d.name}`,
      onchange: (e) => { const v = e.target.value.trim(); if (v) saveDs(t, { name: v }); else renderConfig(true); } }),
      el("div", { class: "tmeta" }, el("code", {}, t), " · ", el("span", { class: "turl" }, k ? k.url : ""), " · ", el("span", { class: "tkey " + (keyed ? "ok" : "no") }, keyed ? "key set" : "no key"))),
    Object.entries(TIER).map(([m, tn]) => el("label", { class: "f" }, tn + " ",
      modelPick(t, d.models[m], { "data-m": m, "data-k": `m:${t}:${m}`, "aria-label": `${d.name} model for ${m}` },
        // a model without the slot's window falls back to Now
        (v) => { const c = cur(); saveDs(t, { models: { ...c.models, [m]: v }, ...(valar && { windows: { ...c.windows, [m]: windowsFor(v).includes(c.windows[m]) ? c.windows[m] : "asap" } }) }); }),
      valar && el("span", { class: "seg win", role: "group", "aria-label": `${d.name} completion window for ${m}` },
        windowsFor(d.models[m]).map((w) => el("button", { type: "button", "data-k": `w:${t}:${m}:${w}`, "aria-pressed": String(d.windows[m] === w),
          onclick: () => { const c = cur(); if (c.windows[m] !== w) saveDs(t, { windows: { ...c.windows, [m]: w } }); } }, WIN[w]))))),
    !valar && el("div", { class: "tprices" }, "USD per 1M tokens", priceInputs(d.name, d.prices, (pk) => ({ "data-k": `p:${t}:${pk}`,
      onchange: (e) => {
        const p = readPrices([...e.target.closest(".tprices").querySelectorAll("input")]);
        showErr(pErr, p.error || "");  // shown in place: a re-render would wipe the half-filled fields
        if (!p.error) saveDs(t, { prices: p.prices });
      } })), !d.prices && el("span", {}, "unpriced"), pErr),
    el("div", { class: "tacts" }, keyIn,
      el("button", { type: "button", class: "mini", "data-k": "ks:" + t, onclick: () => {
        const v = keyIn.value.trim();
        if (v) act(async () => { await dsCall("PUT", `downstreams/${encodeURIComponent(t)}/key`, { key: v }, t); keyIn.value = ""; });
      } }, "Replace key"),
      el("button", { type: "button", class: "mini del", "data-k": "del:" + t, onclick: () => {
        if (confirm(`Delete ${d.name}? Its key goes too. Its history stays, shown as "${t}, removed".`)) act(() => dsCall("DELETE", "downstreams/" + encodeURIComponent(t), undefined, t));
      } }, "Delete")),
    re && el("div", { class: "err tacts", role: "alert" }, re.refs ? [`${cur().name} is still in use. Point these elsewhere first:`, el("ul", {}, re.refs.map((x) => el("li", {}, x)))] : re.msg),
    el("p", { class: "warn", role: "status", hidden: !WARN[t] }, WARN[t] || ""));
}
const WIN = { asap: "Now", priority: "Priority", standard: "Standard" };
const WINDOWS = {};  // model id → completion windows it has a price for (absent until the models response says: all)
const windowsFor = (id) => WINDOWS[id] ? Object.keys(WIN).filter((w) => WINDOWS[id].includes(w)) : Object.keys(WIN);
const MODELS = {}, WARN = {}, ASKED = new Set();  // track → model ids from it, once loaded; → a note on its row; asked already
// A real dropdown once the track's models are in; a text box until then, or when the downstream can't be reached.
function modelPick(t, value, attrs, onPick) {
  const ids = MODELS[t];
  if (!ids) return el("input", { ...attrs, value, spellcheck: "false", autocomplete: "off", onchange: (e) => onPick(e.target.value.trim()) });
  return el("select", { ...attrs, onchange: (e) => onPick(e.target.value) },
    (ids.includes(value) ? ids : [value, ...ids]).map((m) => el("option", { value: m, selected: m === value }, m)));
}
// GET /ui/api/models?track=…: the router asks the downstream with its key; the key never reaches the page
function loadModels(t) {
  ASKED.add(t);
  api("GET", "models?track=" + encodeURIComponent(t), undefined, 15000).then((j) => {
    MODELS[t] = j.models;
    Object.assign(WINDOWS, j.windows);
    WARN[t] = j.stale ? `${j.stale}. Showing the saved model list (models.json).` : "";
    renderConfig(true);
  }, (e) => { WARN[t] = `Couldn't load models (${e.message}). Type a model id.`; renderConfig(true); });
}

/* the wizard: connect (and test), a model per tier, prices for a generic one, save. A failed test can still be saved. */
const W = { list: null, kind: null, models: {}, idTouched: false };  // list: model ids from the last good test
const slug = (n) => n.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^[^a-z]+/, "").slice(0, 24).replace(/-+$/, "");
const wPrices = priceInputs("New downstream", null, () => ({}));
$("wPrices").replaceChildren(...wPrices);
function wizOpen(open) {
  $("wiz").hidden = !open;
  $("addDs").setAttribute("aria-expanded", String(open));
  for (const id of ["wName", "wId", "wUrl", "wKey"]) $(id).value = "";
  if (!open) { $("addDs").focus(); return; }
  for (const x of wPrices) x.querySelector("input").value = "";
  Object.assign(W, { list: null, kind: null, models: Object.fromEntries(Object.keys(TIER).map((m) => [m, ""])), idTouched: false });
  $("wOne").checked = false;
  $("wTestR").replaceChildren();
  $("wCost").hidden = false;
  showErr($("wErr"), "");
  wizReuse(); wizModels();
  $("wName").focus();
}
// Stored rows under this id (it was deleted earlier) will show under the new downstream.
function wizReuse() {
  const id = $("wId").value.trim(), old = id && S && S.summary && Object.hasOwn(S.summary.tracks, id) && gone(id);
  $("wReuse").hidden = !old;
  $("wReuse").textContent = old ? `History from an earlier ${id} will show under this one.` : "";
}
function wizModels() {
  const one = $("wOne").checked, pick = (m, label) => {
    const v = W.models[m], set = (x) => { for (const k of one ? Object.keys(TIER) : [m]) W.models[k] = x; };
    const attrs = { "aria-label": label + " model", "data-k": "wm:" + m + (one ? ":all" : "") };
    return el("label", { class: "f" }, label + " ", W.list
      ? el("select", { ...attrs, onchange: (e) => set(e.target.value) }, el("option", { value: "", selected: !v }, "pick a model"),
        (v && !W.list.includes(v) ? [v, ...W.list] : W.list).map((x) => el("option", { value: x, selected: x === v }, x)))
      : el("input", { ...attrs, value: v, spellcheck: "false", autocomplete: "off", placeholder: "model id", onchange: (e) => set(e.target.value.trim()) }));
  };
  keepFocus(() => $("wModels").replaceChildren(...(one ? [pick("fable", "Every tier")] : Object.entries(TIER).map(([m, tn]) => pick(m, tn)))));
}
$("addDs").addEventListener("click", () => wizOpen($("wiz").hidden));
$("wCancel").addEventListener("click", () => wizOpen(false));
$("wiz").addEventListener("keydown", (e) => { if (e.key === "Escape") wizOpen(false); });
$("wName").addEventListener("input", () => { if (!W.idTouched) { $("wId").value = slug($("wName").value); wizReuse(); } });
$("wId").addEventListener("input", () => { W.idTouched = true; wizReuse(); });
$("wOne").addEventListener("change", () => {
  if ($("wOne").checked) for (const m of Object.keys(TIER)) W.models[m] = W.models.fable;
  wizModels();
});
$("wTest").addEventListener("click", async () => {
  const r = $("wTestR");
  r.className = "wtest";
  r.textContent = "Testing…";
  try {
    const p = await api("POST", "downstreams/probe", { url: $("wUrl").value, key: $("wKey").value }, 15000);
    const valar = p.kind === "valar" ? " · looks like Valar (it prices itself)" : "";
    r.className = "wtest " + (p.ok ? "ok" : "no");
    r.textContent = p.ok ? `✓ Reachable · ${plural(p.models.length, "model")}${valar}` : `✗ Not reachable: ${p.reason}. You can still save it.${valar}`;
    W.list = p.ok && p.models.length ? p.models : null;
    W.kind = p.kind;
    if (W.list && W.list.length === 1) { $("wOne").checked = true; for (const m of Object.keys(TIER)) W.models[m] = W.list[0]; }
    $("wCost").hidden = p.kind === "valar";
    wizModels();
  } catch (e) {
    r.className = "wtest no";
    r.textContent = "Couldn't test: " + e.message;
  }
});
$("wSave").addEventListener("click", async () => {
  const name = $("wName").value.trim(), id = $("wId").value.trim(), err = $("wErr");
  if (!name) { showErr(err, "Give it a name."); $("wName").focus(); return; }
  if (Object.values(W.models).some((m) => !m)) { showErr(err, "Pick a model for every tier."); return; }
  const p = $("wCost").hidden ? { prices: null } : readPrices(wPrices.map((x) => x.querySelector("input")));
  if (p.error) { showErr(err, p.error); return; }
  $("wSave").disabled = true;
  showErr(err, "");
  try {
    await dsCall("POST", "downstreams", { id, name, url: $("wUrl").value, key: $("wKey").value, models: { ...W.models }, prices: p.prices }, id);
    $("wKey").value = "";
    wizOpen(false);
  } catch (e) {
    showErr(err, e.status === 400 || e.status === 409 ? e.message : "Couldn't save: " + e.message);
  } finally {
    $("wSave").disabled = false;
  }
});

/* rulebook */
const openCls = { v: false };
const DEFAULT_TEXT = {
  servertool: ["when servertool go anthropic",
    () => ["web search, web fetch, other tools Anthropic runs itself & Claude Code's transcript checks → ", trk("anthropic")]],
  tags: ["when tag is planning go anthropic\nwhen tag is execution go valar",
    () => [el("b", {}, "#planning"), " → ", trk("anthropic"), ", ", el("b", {}, "#execution"), " → ", trk("valar")]],
  tier: ["when tier is opus or tier is fable go anthropic\nwhen tier is sonnet or tier is haiku or tier is other go valar",
    () => ["opus & fable → ", trk("anthropic"), ", sonnet, haiku & anything else → ", trk("valar")]],
};
const lines = (text) => text.split("\n").map((s) => s.trim()).filter(Boolean);
// "Can send your main session off Anthropic": a line going to a downstream whose condition never looks at tier or subagent.
const GO_OFF = /\bgo\s+(?!anthropic\s*$)[\w-]+\s*$/i;
const risky = (r) => r.kind === "dsl" && lines(r.text).some((l) => GO_OFF.test(l) && !/\btier\b|\bsubagent\b/i.test(l.replace(GO_OFF, "")));

function describe(r) {
  if (r.kind === "classifier") return el("div", { class: "rdesc" }, `A small model on ${NAME(cfg.classifier.upstream)} reads your newest message once per turn and labels it.`);
  const d = DEFAULT_TEXT[r.builtin];
  if (d && r.text.trim() === d[0]) return el("div", { class: "rdesc" }, d[1]());
  return el("div", { class: "rdesc" }, lines(r.text).map((l) => {
    const m = l.match(/^when\s+(.*?)\s+go\s+([\w-]+)$/i);
    return el("div", { class: "rline" }, m ? [m[1], " → ", trk(m[2].toLowerCase())] : l);
  }));
}

function classifierBits(r, i) {
  const c = cfg.classifier, pct = Math.round(c.threshold * 100);
  const chips = el("div", { class: "chips" }, c.labels.map((l) => el("span", { class: "chip" }, l.name, " → ", trk(l.track))));
  const lab = el("span", {}, `Only decide when at least ${pct}% sure`);
  const slider = el("input", {
    type: "range", min: 0, max: 100, step: 1, value: pct, "data-k": "thr", "aria-label": "Classifier confidence threshold, percent",
    oninput: (e) => { lab.textContent = `Only decide when at least ${e.target.value}% sure`; },
    onchange: (e) => setCls({ threshold: Number(e.target.value) / 100 }),
  });
  const out = [chips, el("label", { class: "slider" }, lab, slider)];
  if (openCls.v) {
    const setLabel = (j, patch) => setCls({ labels: c.labels.map((l, k) => (k === j ? { ...l, ...patch } : l)) });
    out.push(el("div", { class: "cls" },
      el("div", { class: "row" },
        el("label", {}, "Asks ", el("select", { "data-k": "cup", onchange: (e) => setCls({ upstream: e.target.value }) },
          [...DS().map((d) => d.id), "none"].map((u) => el("option", { value: u, selected: c.upstream === u }, u === "none" ? "none (classifier off)" : NAME(u))))),
        el("label", {}, "Model ", modelPick(c.upstream, c.model, { "data-k": "cmodel" }, (v) => setCls({ model: v }))),
        el("label", {}, "Time limit ", el("input", { type: "number", class: "narrow", min: 0.1, max: 30, step: 0.1, value: c.timeout_s, "data-k": "ctime",
          onchange: (e) => { const v = Number(e.target.value); if (v >= 0.1 && v <= 30) setCls({ timeout_s: v }); else renderConfig(true); } }), " s")),
      c.labels.map((l, j) => el("div", { class: "row" },
        el("input", { class: "lname", value: l.name, "aria-label": "Label name", "data-k": "ln:" + j, onchange: (e) => setLabel(j, { name: e.target.value.trim() }) }),
        el("input", { class: "ldesc", value: l.description, "aria-label": "What this label means", placeholder: "what it means", "data-k": "ld:" + j, onchange: (e) => setLabel(j, { description: e.target.value }) }),
        el("select", { "aria-label": "Track for " + l.name, "data-k": "lt:" + j, onchange: (e) => setLabel(j, { track: e.target.value }) },
          TRACKS().map((t) => el("option", { value: t, selected: l.track === t }, "→ " + NAME(t)))),
        el("button", { type: "button", class: "mini", "aria-label": "Remove label " + l.name, onclick: () => setCls({ labels: c.labels.filter((_, k) => k !== j) }) }, "remove"))),
      el("div", {}, el("button", { type: "button", class: "mini", "data-k": "addlabel", onclick: () => {
        let n = 1; while (c.labels.some((l) => l.name === "label " + n)) n++;
        setCls({ labels: [...c.labels, { name: "label " + n, description: "", track: "anthropic" }] });
      } }, "+ add label"))));
  }
  return out;
}
const setCls = (patch) => save({ ...cfg, classifier: { ...cfg.classifier, ...patch } }, $("rbErr"));

function move(from, to) {
  const rs = [...cfg.rules];
  if (to < 0 || to >= rs.length || from === to) return;
  rs.splice(to, 0, rs.splice(from, 1)[0]);
  save(withRules(rs), $("rbErr"));
}

let dragFrom = null;
function ruleItem(r, i) {
  const n = cfg.rules.length;
  const acts = [];
  if (r.kind === "classifier") {
    acts.push(el("button", { type: "button", class: "mini", "aria-expanded": String(openCls.v), "data-k": "clsedit",
      onclick: () => { openCls.v = !openCls.v; renderConfig(true); } }, openCls.v ? "Done" : "Edit"));
  } else {
    acts.push(el("button", { type: "button", class: "mini", "data-k": "edit:" + r.name, onclick: () => openEditor(i) }, "Edit"));
    if (!r.builtin) acts.push(el("button", { type: "button", class: "mini", onclick: () => {
      if (confirm(`Delete the rule "${r.name}"?`)) save(withRules(cfg.rules.filter((_, k) => k !== i)), $("rbErr"));
    } }, "Delete"));
  }
  const num = el("span", { class: "num", draggable: "true", title: "Drag to reorder", "aria-hidden": "true" }, i + 1);
  const li = el("li", {
    tabindex: 0, class: r.enabled ? "" : "off", "data-k": "li:" + r.name,
    "aria-label": `Rule ${i + 1} of ${n}: ${r.name}${r.enabled ? "" : ", switched off"}. Alt+Up or Alt+Down moves it.`,
  },
    num,
    el("div", { class: "rbody" },
      el("div", { class: "rname" }, r.name, risky(r) && el("span", { class: "badge", title: "A line sends requests to a downstream without checking tier or subagent, so it can move your main (opus) conversation." }, "Can send your main session off Anthropic")),
      describe(r),
      r.kind === "classifier" ? classifierBits(r, i) : null,
      el("div", { class: "hint dec", "data-r": r.kind === "classifier" ? "classifier" : r.name }),
      el("div", { class: "racts" }, acts)),
    el("button", { type: "button", class: "tg", "aria-pressed": String(r.enabled), "aria-label": "Rule on: " + r.name, "data-k": "tg:" + r.name,
      onclick: () => save(withRules(cfg.rules.map((x, k) => (k === i ? { ...x, enabled: !x.enabled } : x))), $("rbErr")) }));
  li.addEventListener("keydown", (e) => {
    if (e.target !== li || !e.altKey || (e.key !== "ArrowUp" && e.key !== "ArrowDown")) return;
    e.preventDefault();
    move(i, i + (e.key === "ArrowUp" ? -1 : 1));
  });
  num.addEventListener("dragstart", (e) => {
    dragFrom = i;
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", r.name);
    e.dataTransfer.setDragImage(li, 14, 14);
    li.classList.add("dragging");
  });
  num.addEventListener("dragend", () => { dragFrom = null; li.classList.remove("dragging"); });
  li.addEventListener("dragover", (e) => { if (dragFrom == null) return; e.preventDefault(); li.classList.add("over"); });
  li.addEventListener("dragleave", () => li.classList.remove("over"));
  li.addEventListener("drop", (e) => { e.preventDefault(); li.classList.remove("over"); if (dragFrom != null) move(dragFrom, i); });
  return li;
}
const renderRules = () => { $("rules").replaceChildren(...cfg.rules.map(ruleItem)); renderCounts(); };

function renderOtherwise() {
  $("otherwise").replaceChildren(...TRACKS().map((t) => el("button", { type: "button", "data-v": t, "data-k": "ow:" + t, style: cv(t),
    "aria-pressed": String(t === cfg.otherwise), onclick: () => t !== cfg.otherwise && save({ ...cfg, otherwise: t }, $("rbErr")) }, NAME(t))));
}

/* settings lines: window and model overrides by session and hour, per downstream */
// Rendered from renderConfig only (a render here needs MODELS/WINDOWS too), and it runs after every save.
function renderSettings() {
  if (!cfg) return;
  const cl = (S && S.collisions) || [], clash = new Set(cl.flatMap((c) => [c.line, c.by]));
  keepFocus(() => {
    $("stLines").replaceChildren(...cfg.settings.map((s, i) => stLine(s, i, clash.has(i + 1))));
    $("stAdd").disabled = !DS().length;
    $("stColl").hidden = !cl.length;
    $("stColl").replaceChildren(...cl.map((c) => el("li", {}, c.text)));
  });
}
const setSettings = (settings) => save({ ...cfg, settings }, $("sttErr"));
const setLine = (i, patch) => setSettings(cfg.settings.map((s, k) => k === i ? { ...s, ...patch } : s));
const moveLine = (from, to) => {
  const rs = [...cfg.settings];
  if (to < 0 || to >= rs.length || from === to) return;
  rs.splice(to, 0, rs.splice(from, 1)[0]);
  setSettings(rs);
};
// A pick that changes what's valid: a window line needs a valar downstream, an offered window and a real model.
function setPart(i, patch) {
  const s = { ...cfg.settings[i], ...patch }, ds = dsOf(s.downstream) || {}, models = ds.models || {};
  if (ds.kind !== "valar") s.set = "model";
  if (s.set === "window") {
    const wins = windowsFor(models[s.tier || "sonnet"]);
    if (!wins.includes(s.value)) s.value = wins[0] || "asap";
  } else if (!s.value || Object.keys(WIN).includes(s.value)) {
    s.value = models[s.tier || "sonnet"] || "";
  }
  setLine(i, { ...patch, downstream: s.downstream, set: s.set, value: s.value });  // patch first: a tier/set change must survive the validity fixes
}
function stLine(s, i, clash) {
  const ds = dsOf(s.downstream), valar = !!(ds && ds.kind === "valar"), models = (ds && ds.models) || {}, last = i === cfg.settings.length - 1;
  const ids = [...s.when.matchAll(/session is (\S+)/gi)].map((m) => m[1]);
  const sel = (k, v, opts, on) => el("select", { "data-k": k, onchange: (e) => on(e.target.value) },
    opts.map(([v2, t]) => el("option", { value: v2, selected: v === v2 }, t)));
  return el("li", { class: "strow" + (clash ? " clash" : ""), "aria-label": `Settings line ${i + 1}: ${s.set} on ${NAME(s.downstream)}` },
    el("span", { class: "num", "aria-hidden": "true" }, i + 1),
    el("div", { class: "stbody" },
      el("div", { class: "stline" },
        el("input", { class: "whenin", value: s.when, placeholder: "always", spellcheck: "false", autocomplete: "off",
          "data-k": "sw:" + i, "aria-label": `Condition for settings line ${i + 1}`,
          onchange: (e) => setLine(i, { when: e.target.value.replace(/\s+$/, "") }) }),
        sel("sd:" + i, s.downstream, DS().map((d) => [d.id, d.name]), (v) => setPart(i, { downstream: v })),
        sel("st:" + i, s.tier || "", [["", "every tier"], ...Object.entries(TIER)], (v) => setPart(i, { tier: v || null })),
        sel("sk:" + i, s.set, [["model", "model"], ...(valar ? [["window", "window"]] : [])], (v) => setPart(i, { set: v })),
        s.set === "window"
          ? sel("sv:" + i, s.value, windowsFor(models[s.tier || "sonnet"]).map((w) => [w, WIN[w]]), (v) => setLine(i, { value: v }))
          : modelPick(s.downstream, s.value, { "data-k": "sv:" + i }, (v) => setLine(i, { value: v })),
        el("span", { class: "stacts" },
          el("button", { type: "button", class: "mini", disabled: i === 0, "aria-label": `Move settings line ${i + 1} up`, onclick: () => moveLine(i, i - 1) }, "↑"),
          el("button", { type: "button", class: "mini", disabled: last, "aria-label": `Move settings line ${i + 1} down`, onclick: () => moveLine(i, i + 1) }, "↓"),
          el("button", { type: "button", class: "mini del", "aria-label": `Delete settings line ${i + 1}`,
            onclick: () => setSettings(cfg.settings.filter((_, k) => k !== i)) }, "Delete"))),
      ids.length ? el("div", { class: "hint shint" }, "sessions: " + ids.map((x) => shortLabel(x)).join(", ")) : null));
}
// A new line goes on top, pre-filled with the picked sessions (the same ones the chips show).
$("stAdd").addEventListener("click", () => {
  const ds = DS().find((d) => d.kind === "valar") || DS()[0];
  if (!ds) return;
  setSettings([{ when: view.pick.filter((x) => x !== "unknown").map((x) => "session is " + x).join(" or "),
    downstream: ds.id, tier: null, set: ds.kind === "valar" ? "window" : "model",
    value: ds.kind === "valar" ? "asap" : (ds.models && ds.models.sonnet) || "" }, ...cfg.settings]);
});

/* doubling strip */
function renderStrip() {
  const d = cfg.doubling, pct = Math.round(d.rate * 100);
  $("dRate").value = pct;
  $("dRateN").value = pct;
  $("dTag").value = d.tag;
  // one row per track: when it answers, which other track to compare with
  $("shadows").replaceChildren(...TRACKS().map((t) => el("label", { class: "srow" }, el("span", { class: "sw", style: cv(t) }), `When ${NAME(t)} answers, compare with `,
    el("select", { "data-k": "sh:" + t, onchange: (e) => setDbl({ shadow: { ...cfg.doubling.shadow, [t]: e.target.value || null } }) },
      [...TRACKS().filter((x) => x !== t), ""].map((x) => el("option", { value: x, selected: (d.shadow[t] || "") === x }, x ? NAME(x) : "off"))))));
}
const setDbl = (patch) => save({ ...cfg, doubling: { ...cfg.doubling, ...patch } }, $("dblErr"));
const setRate = (v) => { v = Math.max(0, Math.min(100, Math.round(Number(v) || 0))); setDbl({ rate: v / 100 }); };
$("dRate").addEventListener("input", (e) => { $("dRateN").value = e.target.value; });
$("dRate").addEventListener("change", (e) => setRate(e.target.value));
$("dRateN").addEventListener("change", (e) => setRate(e.target.value));
$("dTag").addEventListener("change", (e) => setDbl({ tag: e.target.value.trim().replace(/^#+/, "") }));
["dRate", "dRateN", "dTag"].forEach((id) => { $(id).dataset.k = id; });

/* ---------- rule editor ---------- */
let ed = null, edSeq = 0, edTimer = null;
const TOK = /("(?:\\.|[^"\\])*"?)|(\/(?:\\.|[^/\\])*\/?[ims]*)|\b(when|and|or|not|is|contains|matches|go)\b|\b(anthropic)\b|(?<=\bgo\s+)([a-z][\w-]*)|\b(text|tag|tier|model|tokens|subagent|servertool|label)\b/gi;
const TOKCLS = [null, "st", "st", "kw", "an", "ds", "fd"];
function highlight() {
  const src = $("edText").value, out = [];
  let i = 0;
  for (const m of src.matchAll(TOK)) {
    if (m.index > i) out.push(src.slice(i, m.index));
    const g = m.findIndex((x, k) => k && x !== undefined);  // a word after "go" shows in that downstream's colour
    out.push(el("span", { class: TOKCLS[g], style: g === 5 && dsOf(m[0].toLowerCase()) ? cv(m[0].toLowerCase()) : null }, m[0]));
    i = m.index + m[0].length;
  }
  out.push(src.slice(i) + "\n");
  $("edHl").replaceChildren(...out);
  const ta = $("edText");
  ta.style.height = "auto";
  ta.style.height = ta.scrollHeight + "px";
}

function openEditor(i) {
  const r = i == null ? null : cfg.rules[i];
  ed = { orig: r ? r.name : null };
  $("edTitle").textContent = r ? "Edit rule" : "New rule";
  $("edName").value = r ? r.name : "";
  $("edText").value = r ? r.text : "";
  showErr($("edErr"), "");
  $("editor").hidden = false;
  highlight();
  preview();
  (r ? $("edText") : $("edName")).focus();
  $("editor").scrollIntoView({ block: "nearest", behavior: reduceMotion.matches ? "auto" : "smooth" });
}
function closeEditor() {
  const back = ed && ed.orig;
  ed = null;
  $("editor").hidden = true;
  const b = document.querySelector(`[data-k="${CSS.escape(back ? "edit:" + back : "addrule")}"]`);
  if (b) b.focus();
}
$("addRule").dataset.k = "addrule";
$("addRule").addEventListener("click", () => openEditor(null));
$("edCancel").addEventListener("click", closeEditor);
$("editor").addEventListener("keydown", (e) => { if (e.key === "Escape") closeEditor(); });
$("edText").addEventListener("input", () => { highlight(); schedulePreview(); });
$("edText").addEventListener("scroll", () => { $("edHl").scrollTop = $("edText").scrollTop; });
$("edName").addEventListener("input", schedulePreview);
function schedulePreview() { clearTimeout(edTimer); edTimer = setTimeout(preview, 300); }

async function preview() {
  if (!ed) return;
  const text = $("edText").value, msg = $("edMsg"), seq = ++edSeq;
  if (!text.trim()) { msg.className = "edmsg"; msg.replaceChildren("Write one ", el("code", {}, "when … go …"), " per line."); $("edHits").replaceChildren(); return; }
  const name = ed.orig || $("edName").value.trim() || "new rule", pos = edPos();
  if (pos < 0) { msg.className = "edmsg no"; msg.replaceChildren(GONE); $("edHits").replaceChildren(); return; }
  try {
    const p = await api("POST", "preview", { rule: { name, text }, position: pos });
    if (seq !== edSeq) return;
    msg.className = "edmsg ok";
    msg.replaceChildren(`✓ Valid. In your last ${p.count} requests this would have matched ${p.matched}, and changed the track for ${p.changed} of them.`);
    if (p.count < 200) msg.append(el("span", { class: "note" }, p.count ? `That's every request since the router started (it keeps the last 200 in memory only).` : "No requests since the router started, so nothing to check against yet."));
    if (p.slow) msg.append(el("span", { class: "slow" }, "⚠ This rule took over 50 ms to check against those requests. It runs on every request; a simpler pattern would be kinder."));
    $("edHits").replaceChildren(p.rows.length ? el("table", { class: "hits" },
      el("thead", {}, el("tr", {}, el("th", { class: "t" }, "Time"), el("th", { class: "w" }, "Went"), el("th", { class: "g" }, "Would go"), el("th", {}, "First words"))),
      el("tbody", {}, [...p.rows].reverse().map((x) => el("tr", {},
        el("td", {}, x.ts ? hms(x.ts) : "–"),
        el("td", { title: x.why }, `${x.went} · ${shortReason(x.why)}`),
        el("td", { class: x.would !== x.went ? "chg" : "" }, x.would),
        el("td", { title: x.preview }, `“${x.preview}”`))))) : "");
  } catch (e) {
    if (seq !== edSeq) return;
    msg.className = "edmsg no";
    $("edHits").replaceChildren();
    const b = e.body || {};
    if (e.status === 400 && b.line) {
      const src = text.split("\n")[b.line - 1] || "";
      msg.replaceChildren(`Line ${b.line}, column ${b.col}: ${b.error}`,
        el("pre", { class: "ruletext" }, src + "\n" + " ".repeat(Math.max(0, b.col - 1)) + "^"));
    } else msg.replaceChildren(e.status === 400 ? e.message : "Couldn't check the rule: " + e.message);
  }
}

// Resolved by name each time: the Rulebook may be reordered or edited while the editor is open.
// New rules go to the top; an edited rule keeps its place; -1 = the rule being edited is gone.
const edPos = () => (ed.orig == null ? 0 : cfg.rules.findIndex((r) => r.name === ed.orig));
const GONE = "The rule you were editing was deleted or renamed meanwhile. Cancel and start again.";
$("edSave").addEventListener("click", async () => {
  if (!ed) return;
  const name = $("edName").value.trim(), text = $("edText").value.replace(/\s+$/, "");
  if (!name) { showErr($("edErr"), "Give the rule a name."); $("edName").focus(); return; }
  const pos = edPos();
  if (pos < 0) { showErr($("edErr"), GONE); return; }
  const old = ed.orig == null ? null : cfg.rules[pos];
  const rule = { name, kind: "dsl", enabled: old ? old.enabled : true, text, builtin: old ? old.builtin : null };
  const rs = [...cfg.rules];
  if (old) rs[pos] = rule; else rs.unshift(rule);
  if (await save(withRules(rs), $("edErr"))) { ed.orig = name; closeEditor(); }
});

/* ---------- departures ---------- */
let lastId = 0, minId = null, firstLoad = true;
const LIVE_MAX = 300;
// ids are taken when a request starts but rows are written when it ends, so a long request can land after
// later ones: re-ask for the last OVERLAP ids and slot late rows in by id.
const OVERLAP = 50;
function shortReason(reason, row) {
  if (!reason) return "";
  if (reason === "lever") return "lever";
  if (reason === "default") return "otherwise";
  if (reason.startsWith("about:")) return "about: " + reason.slice(6);
  if (reason.startsWith("rule:")) {
    let n = reason.slice(5);
    const rules = cfg ? cfg.rules : [];
    if (!rules.some((r) => r.name === n)) n = n.replace(/:\d+$/, "");
    const r = rules.find((x) => x.name === n);
    if (r && r.builtin === "tags") return "tag";
    if (r && r.builtin === "tier" && row) return "model: " + tierOf(row.model_in);
    return n;
  }
  return reason;
}
// What a settings line changed on this request, stored as JSON on the row: "window: priority", each line's when in the title.
function ovr(o) {
  let list = null;
  try { list = JSON.parse(o); } catch (_) { return null; }  // unreadable: nothing to show
  if (!Array.isArray(list) || !list.length) return null;
  return el("span", { class: "ovr", title: list.map((x) => `${x.set}: ${x.value} · when ${x.when || "always"}`).join("\n") },
    list.map((x) => x.set + ": " + x.value).join(" · "));
}
function why(r) {
  if (isErr(r.status)) {
    // ponytail: "not configured" reads today's key state, not the state when the request ran
    const k = S && S.downstreams[r.track];
    if (gone(r.track)) return `${label(r.track)} · ${r.status || "no answer"}`;
    if (r.track !== "anthropic" && r.status === 502) return `${label(r.track)} ${k && !k.key_set ? "not configured" : "down"} · 502`;
    return `${label(r.track)} error · ` + (r.status || "no answer");
  }
  return shortReason(r.reason, r);
}
function depRow(r) {
  const err = isErr(r.status);
  const head = el("div", { class: "dephead", role: "button", tabindex: 0, "aria-expanded": "false", "data-k": "dep:" + r.id },
    el("time", { datetime: new Date(r.ts * 1000).toISOString() }, hms(r.ts)),
    el("span", { class: "sw" + (err ? " x" : ""), style: cv(r.track), role: "img", "aria-label": err ? "error" : label(r.track) }),
    el("span", { class: "what" }, el("span", { class: "stag", "data-sid": r.session_id || "" }, shortLabel(r.session_id)), r.preview || "(no text)"),
    el("span", { class: "why" + (err ? " x" : "") }, why(r), r.override && ovr(r.override), r.turn_id && el("span", { class: "dbl", title: "Doubled for comparison", "aria-label": ", doubled" }, "⇄"),
      r.ms != null && el("span", { class: "lat", title: `first byte ${fmtMs(r.ttfb_ms)} · total ${fmtMs(r.ms)}` }, "latency: " + fmtMs(r.ms))));
  const row = el("div", { class: "dep", "data-id": r.id }, head), toggle = () => toggleDep(row, head, r);
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  return row;
}
async function toggleDep(row, head, r) {
  const open = head.getAttribute("aria-expanded") !== "true";
  head.setAttribute("aria-expanded", String(open));
  row.classList.toggle("open", open);
  if (!open) {
    row.querySelector(".explain")?.remove();
    return;
  }
  const box = el("div", { class: "explain" }, "Loading…");
  row.append(box);
  try {
    box.replaceChildren(...explain(await api("GET", "requests/" + r.id)));
  } catch (e) {
    box.textContent = "Couldn't load this request: " + e.message;
  }
}
function explain(d) {
  const ex = Array.isArray(d.explanation) ? d.explanation : [];
  const out = [el("ol", {}, ex.map((it) => el("li", {}, it.rule, " — ",
    it.outcome === "not reached" ? el("s", {}, it.outcome) : it.decided ? el("b", {}, it.outcome) : it.outcome)))];
  const money = priced(d.track, d.cost, d.api_cost, true);
  out.push(el("div", { class: "facts" }, `${d.model_in} → ${d.model_out} · status ${d.status || "none"} · first byte ${fmtMs(d.ttfb_ms)} · total ${fmtMs(d.ms)}`,
    money && [" · ", el("b", {}, money)]));
  out.push(el("div", { class: "facts" }, ["session " + fullLabel(d.session_id),
    d.in_tok != null && [`${tok(d.in_tok)} in / ${tok(d.out_tok)} out`,
      d.cache_read_tok > 0 && `${tok(d.cache_read_tok)} cache read`,
      d.cache_write_tok > 0 && `${tok(d.cache_write_tok)} cache write` + (d.cache_write_1h_tok > 0 ? ` (${tok(d.cache_write_1h_tok)} 1h)` : "")
    ].filter(Boolean).join(" · ")].filter(Boolean).join(" · ")));
  if (d.step) out.push(el("div", { class: "facts" }, "Tool results sent back in this step"), el("pre", { class: "prompt" }, d.step));
  if (d.prompt) out.push(el("div", { class: "facts" }, d.step ? "The prompt it's working on" : "Prompt"), el("pre", { class: "prompt" }, d.prompt));
  if (d.turn_id) {
    let s = null;
    try { s = d.shadow ? JSON.parse(d.shadow.shadow) : null; } catch (_) { /* unreadable pair */ }
    const to = d.shadow && d.shadow.shadow_track;
    const what = !s ? "the other answer hasn't arrived yet"
      : s.skipped ? "skipped, " + s.skipped
      : s.error || isErr(s.status) ? `error: ${s.error || "status " + s.status} after ${fmtMs(s.ms)}`
      : `${label(to)} answered in ${fmtMs(s.ms)} (status ${s.status})`;
    out.push(el("div", { class: "sh" }, `Doubled${to ? " to " + label(to) : ""} for comparison · ${what} · `,
      el("button", { type: "button", class: "mini", onclick: () => showTurn(d.turn_id) }, "see side by side")));
  }
  return out;
}

function addDeps(rows) {
  const wasFirst = firstLoad;
  firstLoad = false;
  const box = $("deps");
  // a late row below minId while "Load older" is offered belongs to that older page, not the live list
  rows = rows.filter((r) => !box.querySelector(`.dep[data-id="${r.id}"]`) && !(r.id < minId && !$("older").hidden));
  if (!rows.length) { $("depEmpty").hidden = !!box.children.length; return; }
  for (const r of rows) {  // newest first; each goes above the first shown row with a lower id
    const n = depRow(r);
    if (!wasFirst) n.classList.add("new");
    const below = [...box.children].find((c) => Number(c.dataset.id) < r.id);
    box.insertBefore(n, below || null);
  }
  lastId = Math.max(lastId, rows[0].id);
  if (minId == null) { minId = rows[rows.length - 1].id; $("older").hidden = rows.length < 200; }
  else minId = Math.min(minId, rows[rows.length - 1].id);
  // ponytail: live rows capped at LIVE_MAX so a long-open tab stays light; once "Load older" was used we keep
  // everything the user asked for (trimming then would leave a gap between live and older rows).
  if (!box.querySelector(".older") && box.children.length > LIVE_MAX) {
    while (box.children.length > LIVE_MAX) box.lastElementChild.remove();
    minId = Number(box.lastElementChild.dataset.id);
    $("older").hidden = false;
  }
  $("depEmpty").hidden = true;
  if (!wasFirst) animate([...rows].reverse());
}
$("older").addEventListener("click", async () => {
  const g = viewGen;
  try {
    const rows = await api("GET", "departures" + qs(sessQ(), spanQ(), "before=" + minId));
    if (g !== viewGen) return;  // the pick changed: these rows belong to the old list
    $("deps").append(...rows.map((r) => { const n = depRow(r); n.classList.add("older"); return n; }));
    if (rows.length) minId = rows[rows.length - 1].id;
    $("older").hidden = rows.length < 200;
  } catch (e) {
    if (g === viewGen) $("offline").hidden = false;
  }
});

/* ---------- the yard: one dot per real request ---------- */
function animate(rows) {
  if (reduceMotion.matches) return;
  rows.slice(-12).forEach((r, k) => setTimeout(() => {
    run(r.track, isErr(r.status) ? "x" : "");
    const sh = r.turn_id && cfg && cfg.doubling.shadow[r.track];
    if (sh) run(sh, "s");
  }, k * 220));
}
function run(track, cls) {
  const p = YARD[track] && YARD[track].ghost;
  if (!p) return;  // a removed track has no line
  const len = p.getTotalLength();
  const c = el("circle", { class: "dot " + cls, style: cv(track), r: 6, cx: -20, cy: -20 }), base = cls === "s" ? 0.55 : 1;
  $("dots").append(c);
  const t0 = performance.now(), dur = 2600;
  const f = (now) => {
    const k = Math.min(1, (now - t0) / dur), e = k < 0.5 ? 2 * k * k : 1 - (2 - 2 * k) ** 2 / 2;
    const pt = p.getPointAtLength(len * e);
    c.setAttribute("cx", pt.x);
    c.setAttribute("cy", pt.y);
    c.style.opacity = String(base * (k > 0.9 ? (1 - k) * 10 : 1));
    if (k < 1) requestAnimationFrame(f); else c.remove();
  };
  requestAnimationFrame(f);
}

/* ---------- side by side ---------- */
let turns = [], curTurn = null, turnData = null, stepIdx = 0, turnKey = "";
// Every await below re-checks viewGen: a reply for the previous pick must not land in the new one.
async function loadTurns() {
  const g = viewGen, list = await api("GET", "turns" + qs(sessQ()));
  if (g !== viewGen) return;
  turns = list;
  const n = view.pick.length, real = turns.filter((t) => t.ts);
  if (n) $("dblCount").textContent = `${plural(real.length, "doubled turn")} from the ${n === 1 ? "picked session" : n + " picked sessions"} · you've judged ${real.filter((t) => t.verdict).length}`;
  if (curTurn && !turns.some((t) => t.id === curTurn)) turns.unshift({ id: curTurn });
  if (!curTurn && turns.length) curTurn = turns[0].id;
  await loadTurn();
}
async function loadTurn() {
  if (!curTurn) { turnData = null; turnKey = ""; return renderTurn(); }
  const g = viewGen;
  let d;
  try { d = await api("GET", "turns/" + encodeURIComponent(curTurn)); } catch (e) {
    if (g !== viewGen) return;
    if (e.status !== 404) throw e;
    turns = turns.filter((t) => t.id !== curTurn);
    curTurn = turns.length ? turns[0].id : null;
    return loadTurn();
  }
  if (g !== viewGen) return;
  const key = JSON.stringify(d);
  if (key === turnKey) return;
  const ids = new Set((d.steps || []).map((x) => x.request_id));
  for (const id of REQ.keys()) if (!ids.has(id)) REQ.delete(id);  // keep only the shown turn's rows
  if (!turnData || turnData.id !== d.id) stepIdx = 0;
  turnData = d;
  turnKey = key;
  renderTurn();
}
async function showTurn(id) {
  curTurn = id;
  turnKey = "";
  const g = viewGen;
  await loadTurns();
  if (g !== viewGen) return;
  $("pair").scrollIntoView({ behavior: reduceMotion.matches ? "auto" : "smooth" });
}
// Picked sessions: count the verdicts on their turns (side by side ignores the time span).
// ponytail: the turns list holds the newest 100, so older verdicts drop out of a picked tally.
function tally() {
  if (!view.pick.length) return S ? S.stats.tally : {};
  const t = {};
  for (const x of turns) if (x.verdict) t[x.verdict] = (t[x.verdict] || 0) + 1;
  return t;
}
const tallyText = () => { const t = tally(); return "So far: " + [...new Set([...TRACKS(), ...Object.keys(t).filter((k) => k !== "same")]), "same"].map((k) => `${k === "same" ? "same" : label(k)} ${t[k] || 0}`).join(" · "); };

// request id → its row, for the served side's cost (the pair stores only the shadow's); null while loading.
// A pair can be written before its request row, so a failed fetch isn't cached: it's retried a few seconds later.
// A Valar row without a cost may get one from the hourly reprice: it's kept but fetched again a minute later
// (one timer per fetched row; it stops once the turn leaves the screen, which prunes the entry).
const REQ = new Map();
function fetchRow(id, prev) {
  api("GET", "requests/" + id).then((r) => {
    if (REQ.get(id) !== prev) return;  // pruned or replaced meanwhile
    REQ.set(id, r);
    renderTurn();
    if (r.track !== "anthropic" && r.cost == null) setTimeout(() => { if (REQ.get(id) === r) fetchRow(id, r); }, 60000);
  }, () => {
    if (prev) return setTimeout(() => { if (REQ.get(id) === prev) fetchRow(id, prev); }, 60000);  // keep what we have
    setTimeout(() => { if (REQ.get(id) === null) { REQ.delete(id); renderTurn(); } }, 3000);
  });
}
function servedRow(id) {
  if (!REQ.has(id)) { REQ.set(id, null); fetchRow(id, null); }
  return REQ.get(id);
}
function card(track, step) {
  const served = step.served_track === track;
  let s = {};
  try { s = JSON.parse(served ? step.served : step.shadow) || {}; } catch (_) { s.error = "unreadable"; }
  const r = served ? servedRow(step.request_id) : null;
  const money = served ? r && priced(track, r.cost, r.api_cost) : priced(track, s.cost, null);
  const meta = el("div", { class: "meta" }, el("span", { class: "tn" }, label(track)), el("span", {}, fmtMs(s.ms)),
    s.output_tokens != null && el("span", {}, s.output_tokens + " tokens"), money && el("span", {}, money),
    served && el("span", { class: "served" }, "sent to Claude Code"));
  const body = [];
  if (s.skipped === "busy") body.push(el("p", { class: "state" }, "skipped, busy: this track was already full with other work"));
  else if (s.skipped) body.push(el("p", { class: "state x" }, "skipped: " + s.skipped));
  else {
    if (s.error || isErr(s.status) || s.status >= 400) body.push(el("p", { class: "state x" }, `error: ${s.error || "status " + s.status}`));
    if (s.text) body.push(el("p", { class: "atext" }, s.text));
    if (s.tools && s.tools.length) body.push(el("ul", { class: "tools" }, s.tools.map((t) => {
      let inp = t.input;
      try { inp = JSON.stringify(JSON.parse(t.input)); } catch (_) { /* partial JSON: show as is */ }
      return el("li", { title: t.input }, el("b", {}, t.name), " ", (inp || "").slice(0, 200));
    })));
    if (!s.text && !(s.tools && s.tools.length) && !s.error) body.push(el("p", { class: "state" }, "(empty answer)"));
  }
  return el("div", { class: "ans", style: cv(track) }, meta, body);
}

function renderTurn() {
  const box = $("turn");
  keepFocus(() => {
    if (!turnData) {
      const on = cfg && cfg.doubling.rate > 0;
      box.replaceChildren(el("p", { class: "hint empty" }, on ? "No doubled turns yet. The next chosen turn shows up here."
        : "Doubling is off. Turn the rate up to send a copy of some turns to a second track and compare the answers here. Pick the tracks above."));
      return;
    }
    const d = turnData, steps = d.steps || [], ti = turns.findIndex((t) => t.id === d.id), st = steps[stepIdx];
    const nav = (label, k, dis, fn) => el("button", { type: "button", disabled: dis, "data-k": k, onclick: fn }, label);
    // the pair's tracks (a turn's steps can in principle land on different tracks)
    const pairT = [...new Set(steps.flatMap((x) => [x.served_track, x.shadow_track]).filter(Boolean))];
    const verdicts = [...pairT.map((t) => [t, label(t)]), ["same", "About the same"]];
    box.replaceChildren(
      el("div", { class: "turnhead" },
        el("span", { class: "q", title: d.preview }, el("span", { class: "stag", "data-sid": d.session_id || "" }, shortLabel(d.session_id)), `“${d.preview || "(no text)"}”`),
        d.ts && el("span", { class: "hint" }, hms(d.ts)),
        el("span", { class: "nav", role: "group", "aria-label": "Steps" },
          nav("‹", "sprev", stepIdx <= 0, () => { stepIdx--; renderTurn(); }),
          el("span", { "aria-live": "polite" }, steps.length ? `step ${stepIdx + 1} of ${steps.length}` : "no steps yet"),
          nav("›", "snext", stepIdx >= steps.length - 1, () => { stepIdx++; renderTurn(); }))),
      st ? el("div", { class: "twin" }, card(st.served_track, st), card(st.shadow_track, st))
        : el("p", { class: "state" }, "Waiting for both answers to the first step…"),
      el("div", { class: "verdict" }, "Which did better on this turn?",
        verdicts.map(([v, label]) => el("button", { type: "button", "aria-pressed": String(d.verdict === v), "data-k": "v:" + v, onclick: () => judge(v) }, label)),
        el("span", { class: "tally", id: "tally" }, tallyText())),
      el("div", { class: "verdict" },
        el("span", { class: "nav" },
          nav("‹ Previous turn", "tprev", ti <= 0, () => goTurn(ti - 1)),
          el("span", {}, ti >= 0 ? `turn ${ti + 1} of ${turns.length}, unjudged first` : ""),
          nav("Next turn ›", "tnext", ti < 0 || ti >= turns.length - 1, () => goTurn(ti + 1))),
        el("span", { class: "tally" }, el("button", { type: "button", class: "mini", onclick: forget }, "Forget all pairs"))));
  });
}
function goTurn(i) { curTurn = turns[i].id; turnKey = ""; loadTurn().catch(() => { $("offline").hidden = false; }); }
async function judge(v) {
  try {
    await api("PUT", `turns/${encodeURIComponent(turnData.id)}/verdict`, { verdict: v });
    turnData.verdict = v;
    const t = turns.find((x) => x.id === turnData.id);
    if (t) t.verdict = v;
    renderTurn();
    tick();
  } catch (e) {
    showErr($("dblErr"), "Couldn't save the verdict: " + e.message);
  }
}
async function forget() {
  if (!confirm("Forget all doubled pairs? Every stored answer and verdict is deleted.")) return;
  try {
    await api("DELETE", "pairs");
    turns = []; curTurn = null; turnData = null; turnKey = "";
    renderTurn();
    tick();
  } catch (e) {
    showErr($("dblErr"), "Couldn't forget the pairs: " + e.message);
  }
}

/* ---------- go ---------- */
renderSpan();
// sessions first, so the first departures already carry their labels
loadSessions().catch(() => {}).finally(() => {
  viewChanged(true);  // also sets the departures hint for a remembered pick
  setInterval(tick, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) { tick(); loadSessions().catch(() => {}); } });
});
