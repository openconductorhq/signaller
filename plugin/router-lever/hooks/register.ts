import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const LEVER = `${BASE}/ui/api/lever`
const NAME = `${BASE}/ui/api/name`
const SETTINGS = `${BASE}/ui/api/settings`
let MODES = ['rules', 'anthropic'] // the router's list, refreshed by every lever answer
const HEADERS = { origin: BASE, 'content-type': 'application/json' } // the console takes writes from its own origin only

type Lever = { lever: string; session: string | null; global: string; modes?: string[] }
type Line = { downstream: string; tier: string | null; set: string; value: string }
type Form = { ds: string; tier: string | null; key: 'window' | 'model'; value: string } | { ds: string; reset: true }

// this session's lever: its own if /router set one, else the router's global one
const lever = async ($: EngineInterface, set?: { lever: string | null }): Promise<Lever> => {
  const session = await $.session.id()
  const r = set
    ? await $.http.fetch(LEVER, { method: 'PUT', headers: HEADERS, body: JSON.stringify({ session, ...set }) })
    : await $.http.fetch(`${LEVER}?session=${encodeURIComponent(session)}`)
  if (!r.ok) throw new Error(`the router answered ${r.status}: ${r.text.slice(0, 200)}`)
  const l: Lever = JSON.parse(r.text)
  if (Array.isArray(l.modes) && l.modes.length) MODES = l.modes
  return l
}

// what the router console calls this session; empty clears it
const rename = async ($: EngineInterface, name: string): Promise<string | null> => {
  const session = await $.session.id()
  const r = await $.http.fetch(NAME, { method: 'PUT', headers: HEADERS, body: JSON.stringify({ session, name }) })
  if (!r.ok) throw new Error(`the router answered ${r.status}: ${r.text.slice(0, 200)}`)
  return JSON.parse(r.text).name
}

// a settings form: "<downstream> [<tier>] window|model <value>", or "<downstream> reset". Downstream,
// tier and key match without case; the value keeps its case, model ids are case-sensitive. The value ends
// before trailing punctuation (a sentence's "." is not part of a model id) and "reset" must be a whole
// word ("resetting" is not a settings form).
const TAIL = '([a-z0-9_-]+)(?:\\s+(fable|opus|sonnet|haiku))?\\s+(?:(window|model)\\s+(\\S+?)(?=[.,;:!?)]*(?:\\s|$))[.,;:!?)]*|(reset)(?![a-z0-9_-]))'
const FORM = new RegExp(`^${TAIL}$`, 'i') // the command's arguments, and the tool's settings input
const form = (m: RegExpExecArray): Form => {
  const ds = m[1]!.toLowerCase()
  return m[5] ? { ds, reset: true } : { ds, tier: m[2]?.toLowerCase() ?? null, key: m[3]!.toLowerCase() as 'window' | 'model', value: m[4]! }
}
const parseForm = (s: string): Form | null => {
  const m = FORM.exec(s.trim())
  return m ? form(m) : null
}
const canon = (f: Form): string => ('reset' in f ? `${f.ds} reset` : `${f.ds} ${f.tier ?? '*'} ${f.key} ${f.value}`)
const downstreams = () => MODES.filter(m => m !== 'rules' && m !== 'anthropic')

// Claude may switch the lever or change settings only on the person's say-so: each "/router <mode>" they
// type, in any prompt (e.g. a multi-step plan), allows Claude one switch to that mode. A typed settings
// form instead allows exactly one write of exactly those values — and never also a lever switch to that
// downstream. Later prompts don't erase either. Kept in memory; a reload forgets them, which only denies.
const granted: string[] = []
const grantedSettings: string[] = []
const GRANT = /(?:^|\s)\/router\s+([a-z0-9_-]+)/gi
const GRANT_SETTINGS = new RegExp(`(?:^|\\s)\\/router\\s+${TAIL}`, 'gi') // the same form, as typed in a prompt
// "/router <downstream> reset…" with more than the word "reset" there is a settings attempt that grants nothing
const RESETISH = /(?:^|\s)\/router\s+[a-z0-9_-]+(?:\s+(?:fable|opus|sonnet|haiku))?\s+reset[a-z0-9_-]*/gi
const grant = (text: string) => {
  const settingsForms = new Set<number>()
  for (const m of text.matchAll(GRANT_SETTINGS)) {
    settingsForms.add(m.index!) // both regexes start at the same place: the "/router" they share
    grantedSettings.push(canon(form(m)))
  }
  for (const m of text.matchAll(RESETISH)) settingsForms.add(m.index!) // "resetting" and friends: an attempt, no grant
  for (const m of text.matchAll(GRANT)) {
    if (settingsForms.has(m.index!)) continue // that "/router <word>" was the start of a settings form
    granted.push(m[1]!.toLowerCase()) // checked against the router's modes when used: they may not be known yet
  }
}
const take = (list: string[], want: string) => {
  const i = list.indexOf(want)
  return i >= 0 && list.splice(i, 1).length > 0
}

// set (or reset) this session's settings line; the reply lists the session's resulting lines and any collisions.
// `typed`: the settings form as typed, when the call must be gated on the person's "/router …" (the tool path);
// the command path is the person's own hand and passes nothing. A failed write hands the say-so back.
const setSettings = async ($: EngineInterface, f: Form, typed?: string): Promise<{ ok: boolean; text: string }> => {
  if (!downstreams().includes(f.ds))
    return { ok: false, text: `Unknown downstream "${f.ds}". The router's downstreams: ${downstreams().join(', ') || 'none yet'}.` }
  const c = canon(f)
  if (typed !== undefined && !take(grantedSettings, c))
    return { ok: false, text: `Not setting: the person hasn't typed "/router ${typed}" for this change (each one they type allows one). Only set a line when they tell you to that way; they can also type /router ${typed} themselves.` }
  const session = await $.session.id()
  const body = 'reset' in f
    ? { session, downstream: f.ds, reset: true }
    : { session, downstream: f.ds, tier: f.tier, set: f.key, value: f.value }
  try {
    const r = await $.http.fetch(SETTINGS, { method: 'PUT', headers: HEADERS, body: JSON.stringify(body) })
    if (!r.ok) {
      if (typed !== undefined) grantedSettings.push(c)
      return { ok: false, text: `claude-router answered ${r.status}: ${r.text.slice(0, 200)}` }
    }
    const back = JSON.parse(r.text) as { lines: Line[]; collisions: { text: string }[] }
    if ('reset' in f) return { ok: true, text: `claude-router: this session's ${f.ds} settings cleared` }
    const lines = back.lines.map(l => `claude-router: ${l.downstream} ${l.set} ${l.value} for this session${l.tier ? ` (${l.tier})` : ''}`)
    return { ok: true, text: [...lines, ...back.collisions.map(x => x.text)].join('\n') }
  } catch (err) {
    if (typed !== undefined) grantedSettings.push(c)
    return { ok: false, text: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
  }
}

// switch (or with no mode, read) the lever; the reply text either way, ok false when the router is down
const switchTo = async ($: EngineInterface, want: string): Promise<{ ok: boolean; text: string }> => {
  try {
    const l = await lever($, want ? { lever: want === 'reset' ? null : want } : undefined)
    show($, l)
    return { ok: true, text: `claude-router: ${label(l)}` }
  } catch (err) {
    show($)
    return { ok: false, text: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
  }
}

const label = (l: Lever) => (l.session ? `${l.lever} (this session)` : `${l.lever} (global)`)
const show = ($: EngineInterface, l?: Lever) => $.ui.status(l ? `router: ${label(l)}` : 'router: down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router',
      description:
        "Show or switch this session's claude-router mode; reset goes back to the global one; " +
        '<downstream> [<tier>] window|model <value> sets this session’s settings line, <downstream> reset clears them; name <text> labels it in the console',
      argumentHint: '[mode|reset|name <text>|<downstream> [tier] window|model <value>]',
      immediate: true,
    })
    // the same switch for Claude itself: mcp__router-lever__router_lever
    await $.tool.register({
      name: 'router_lever',
      description:
        "Show or switch claude-router's lever for this Claude Code session (its subagents follow). " +
        'lever: rules (follow the rulebook), anthropic, or a downstream id (send everything there), reset (back to the global lever); omit it to only read the current one. ' +
        'settings: "<downstream> [<tier>] window|model <value>" sets this session’s window or model on a downstream (tier: fable, opus, sonnet or haiku); "<downstream> reset" clears this session’s lines for it. ' +
        'Never switch on your own initiative: only when the person tells you to with "/router <mode>" or "/router <downstream> [<tier>] window|model <value>" in a message (e.g. "do /router anthropic", or a plan with "/router rules" at a later step). ' +
        'Each form they typed allows one use of exactly that form; others are refused.',
      inputSchema: {
        type: 'object',
        properties: { lever: { type: 'string' }, settings: { type: 'string' } },
        additionalProperties: false,
      },
      isDeferred: false,
    })
    const poll = () => lever($).then(l => show($, l), () => show($))
    poll()
    $.clock.every(15_000, poll)
    return next(e)
  })

  on('tool.call', { tool: 'mcp__router-lever__router_lever' }, async ($, e) => {
    const settings = (e as { settings?: string }).settings
    if (settings !== undefined) {
      const f = parseForm(settings)
      if (!f) return { deny: `Unknown settings "${settings}". Use "<downstream> [<tier>] window|model <value>", or "<downstream> reset".` }
      const r = await setSettings($, f, settings)
      return r.ok ? { result: r.text } : { deny: r.text }
    }
    const want = (e as { lever?: string }).lever
    if (want && want !== 'reset' && !MODES.includes(want)) return { deny: `Unknown lever "${want}". Use one of: ${MODES.join(', ')}, or reset.` }
    if (want && !take(granted, want))
      return { deny: `Not switching: the person hasn't typed "/router ${want}" for this switch (each one they type allows one). Only switch when they tell you to that way; they can also type /router ${want} themselves.` }
    const r = await switchTo($, want ?? '')
    if (!r.ok && want) granted.push(want) // the switch didn't happen: the person's say-so still stands
    return r.ok ? { result: r.text } : { deny: r.text }
  })

  on('prompt.submit', ($, e, next) => {
    if (!e.isMeta && !e.command) grant(e.text)
    return next(e)
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const args = e.args.trim()
    if (/^name(\s|$)/i.test(args)) {
      try {
        const n = await rename($, args.slice(4).trim())
        return { text: n ? `claude-router: this session is "${n}" in the console` : 'claude-router: session name cleared' }
      } catch (err) {
        return { text: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
      }
    }
    const f = parseForm(args)
    if (f) return { text: (await setSettings($, f)).text }
    const want = args.toLowerCase()
    if (want && want !== 'reset' && !MODES.includes(want))
      return { text: `Unknown mode "${want}". Use one of: ${MODES.join(', ')}, reset, or name <text>.` }
    return { text: (await switchTo($, want)).text }
  })
}
