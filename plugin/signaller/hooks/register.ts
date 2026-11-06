import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const LEVER = `${BASE}/ui/api/lever`
const NAME = `${BASE}/ui/api/name`
const SETTINGS = `${BASE}/ui/api/settings`
let MODES = ['rules', 'anthropic'] // the router's list, refreshed by every lever answer
const HEADERS = { origin: BASE, 'content-type': 'application/json' } // the console takes writes from its own origin only

type Lever = { lever: string; session: string | null; global: string; modes?: string[]; name?: string | null; prompts?: Record<string, string>; notice?: { turn: string; text: string } | null }
type Line = { downstream: string; tier: string | null; set: string; value: string }
type Form = { ds: string; tier: string | null; key: 'window' | 'model'; value: string } | { ds: string; reset: true }

// this session's lever: its own if /signaller set one, else the router's global one
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

// Claude may switch the lever or change settings only on the person's say-so. The "/signaller <mode>" and
// settings forms a prompt carries become the queue of allowed steps, in the order typed: each call must match
// the next one and uses it up (a plan's "anthropic, rules, anthropic, rules" runs only in that order). A later
// prompt with no "/signaller" keeps the queue (messages sent while Claude works); one with any replaces it, so
// leftovers don't linger. Only the main agent spends the queue: subagents follow the session's lever anyway.
// Kept in the plugin's store under the session id, so a reload or a restart of the same session keeps it.
// ponytail: an ended session's leftover steps stay in the store (tiny, keyed by an id nobody reuses).
const qkey = async ($: EngineInterface) => `pending:${await $.session.id()}`
const queue = async ($: EngineInterface) => (((await $.store.get(await qkey($))) as string[] | undefined) ?? [])
const save = async ($: EngineInterface, q: string[]) => {
  const k = await qkey($)
  await (q.length ? $.store.set(k, q) : $.store.delete(k))
}
// "/signaller run …" and "/signaller name …" are commands, not steps: quoting one in a prompt keeps the queue
const GRANT = /(?:^|\s)\/signaller\s+(?!(?:run|name)(?:\s|$))([a-z0-9_-]+)/gi
const GRANT_SETTINGS = new RegExp(`(?:^|\\s)\\/signaller\\s+${TAIL}`, 'gi') // the same form, as typed in a prompt
// "/signaller <downstream> reset…" with more than the word "reset" there is a settings attempt that grants nothing
const RESETISH = /(?:^|\s)\/signaller\s+[a-z0-9_-]+(?:\s+(?:fable|opus|sonnet|haiku))?\s+reset[a-z0-9_-]*/gi
const grant = async ($: EngineInterface, text: string) => {
  const steps = new Map<number, string | null>() // keyed by where the "/signaller" starts; all three regexes share it
  for (const m of text.matchAll(GRANT_SETTINGS)) steps.set(m.index!, `set ${canon(form(m))}`)
  for (const m of text.matchAll(RESETISH)) if (!steps.has(m.index!)) steps.set(m.index!, null) // "resetting": an attempt, no grant
  for (const m of text.matchAll(GRANT)) if (!steps.has(m.index!)) steps.set(m.index!, `lever ${m[1]!.toLowerCase()}`) // modes are checked when used
  if (!steps.size) return
  await save($, [...steps].sort((a, b) => a[0] - b[0]).flatMap(([, s]) => (s ? [s] : [])))
}
const take = async ($: EngineInterface, want: string) => {
  const q = await queue($)
  if (q[0] !== want) return false
  q.shift()
  await save($, q)
  return true
}
const giveBack = async ($: EngineInterface, step: string) => {
  const q = await queue($)
  q.unshift(step)
  await save($, q)
}
const upNext = (q: string[]) => (q[0] ? ` The next one allowed is "/signaller ${q[0].replace(/^\S+ /, '').replace(' * ', ' ')}".` : '')

// set (or reset) this session's settings line; the reply lists the session's resulting lines and any collisions.
// `typed`: the settings form as typed, when the call must be gated on the person's "/signaller …" (the tool path);
// the command path is the person's own hand and passes nothing. A failed write hands the say-so back.
const setSettings = async ($: EngineInterface, f: Form, typed?: string): Promise<{ ok: boolean; text: string }> => {
  if (!downstreams().includes(f.ds))
    return { ok: false, text: `Unknown downstream "${f.ds}". The router's downstreams: ${downstreams().join(', ') || 'none yet'}.` }
  const c = canon(f)
  if (typed !== undefined && !(await take($, `set ${c}`)))
    return { ok: false, text: `Not setting: "/signaller ${typed}" isn't the next step the person typed (each one they type allows one, in order).${upNext(await queue($))} Only set a line when they tell you to that way; they can also type "/signaller ${typed}" themselves.` }
  const session = await $.session.id()
  const body = 'reset' in f
    ? { session, downstream: f.ds, reset: true }
    : { session, downstream: f.ds, tier: f.tier, set: f.key, value: f.value }
  try {
    const r = await $.http.fetch(SETTINGS, { method: 'PUT', headers: HEADERS, body: JSON.stringify(body) })
    if (!r.ok) {
      if (typed !== undefined) await giveBack($, `set ${c}`)
      return { ok: false, text: `signaller answered ${r.status}: ${r.text.slice(0, 200)}` }
    }
    const back = JSON.parse(r.text) as { lines: Line[]; collisions: { text: string }[] }
    if ('reset' in f) return { ok: true, text: `this session's ${f.ds} settings cleared` }
    const lines = back.lines.map(l => `${l.downstream} ${l.set} ${l.value} for this session${l.tier ? ` (${l.tier})` : ''}`)
    return { ok: true, text: [...lines, ...back.collisions.map(x => x.text)].join('\n') }
  } catch (err) {
    if (typed !== undefined) await giveBack($, `set ${c}`)
    return { ok: false, text: `signaller isn't reachable at ${BASE}: ${(err as Error).message}` }
  }
}

// switch (or with no mode, read) the lever; the reply text either way, ok false when the router is down
const switchTo = async ($: EngineInterface, want: string): Promise<{ ok: boolean; text: string }> => {
  try {
    const l = await lever($, want ? { lever: want === 'reset' ? null : want } : undefined)
    show($, l)
    return { ok: true, text: label(l) }
  } catch (err) {
    show($)
    return { ok: false, text: `signaller isn't reachable at ${BASE}: ${(err as Error).message}` }
  }
}

const label = (l: Lever) => `${l.lever} (${l.session ? 'this session' : 'global'})${l.name ? `, session name: ${l.name}` : ''}`
// an image fallback the router reports for this session: a line in the transcript (shown, never sent to the
// model) and a band above the prompt, once per turn; the first poll after a (re)load only takes note, so an
// old one isn't shown again. The band holds until the person's next prompt clears it.
let seen: string | null | undefined
let band: string | null = null
const warn = ($: EngineInterface, l: Lever) => {
  const n = l.notice
  if (seen !== undefined && n && n.turn !== seen) {
    $.ui.log(`⚠ signaller: ${n.text}`)
    band = n.text
    $.ui.invalidate('ui.render')
  }
  seen = n ? n.turn : null
}
const poll = ($: EngineInterface) => { lever($).then(l => (show($, l), warn($, l)), () => show($)) }
const show = ($: EngineInterface, l?: Lever) => $.ui.status(l ? label(l) : 'down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'signaller',
      description:
        "Show or switch this session's signaller mode; reset goes back to the global one; " +
        '<downstream> [<tier>] window|model <value> sets this session’s settings line, <downstream> reset clears them; name <text> labels it in the console',
      argumentHint: '[mode|reset|name <text>|<downstream> [tier] window|model <value>]',
      immediate: true,
    })
    // the same switch for Claude itself: mcp__signaller__signaller_lever
    await $.tool.register({
      name: 'signaller_lever',
      description:
        "Show or switch signaller's lever for this Claude Code session (its subagents follow). " +
        'lever: rules (follow the rulebook), anthropic, or a downstream id (send everything there), reset (back to the global lever); omit it to only read the current one. ' +
        'settings: "<downstream> [<tier>] window|model <value>" sets this session’s window or model on a downstream (tier: fable, opus, sonnet or haiku); "<downstream> reset" clears this session’s lines for it. ' +
        'Never switch on your own initiative: only when the person tells you to with "/signaller <mode>" or "/signaller <downstream> [<tier>] window|model <value>" in a message (e.g. "do /signaller anthropic", or a plan with "/signaller rules" at a later step). ' +
        'Each form they typed allows one use of exactly that form, in the order they typed them; others are refused, and so is any call from a subagent.',
      inputSchema: {
        type: 'object',
        properties: { lever: { type: 'string' }, settings: { type: 'string' } },
        additionalProperties: false,
      },
      isDeferred: false,
    })
    poll($)
    $.clock.every(60_000, () => poll($)) // idle fallback: a console change shows within a minute
    return next(e)
  })

  // the status line refreshes when it matters: as a turn starts and ends (the main loop's, not each subagent's)
  on('turn.start', ($, e, next) => {
    if (e.agentId === undefined) poll($)
    return next(e)
  })
  on('turn.complete', ($, e, next) => {
    if (e.agentId === undefined) poll($)
    return next(e)
  })

  on('tool.call', { tool: 'mcp__signaller__signaller_lever' }, async ($, e) => {
    const sub = e.agentId !== undefined
    const settings = (e as { settings?: string }).settings
    if (settings !== undefined) {
      const f = parseForm(settings)
      if (!f) return { deny: `Unknown settings "${settings}". Use "<downstream> [<tier>] window|model <value>", or "<downstream> reset".` }
      if (sub) return { deny: 'Not setting: only the main agent changes signaller; a subagent follows its session.' }
      const r = await setSettings($, f, settings)
      return r.ok ? { result: r.text } : { deny: r.text }
    }
    const want = (e as { lever?: string }).lever
    if (want && want !== 'reset' && !MODES.includes(want)) return { deny: `Unknown lever "${want}". Use one of: ${MODES.join(', ')}, or reset.` }
    if (want && sub) return { deny: 'Not switching: only the main agent switches signaller; a subagent follows its session.' }
    if (want && !(await take($, `lever ${want}`)))
      return { deny: `Not switching: "/signaller ${want}" isn't the next step the person typed (each one they type allows one, in order).${upNext(await queue($))} Only switch when they tell you to that way; they can also type "/signaller ${want}" themselves.` }
    const r = await switchTo($, want ?? '')
    if (!r.ok && want) await giveBack($, `lever ${want}`) // the switch didn't happen: the person's say-so still stands
    return r.ok ? { result: r.text } : { deny: r.text }
  })

  on('prompt.submit', async ($, e, next) => {
    if (!e.isMeta && !e.command) await grant($, e.text)
    if (band !== null) {
      band = null
      $.ui.invalidate('ui.render')
    }
    return next(e)
  })

  // the pending image fallback above the prompt; a survey owns the band, so yield to one
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (band === null || e.props.hasSurvey) return next(e)
    const { Text } = $.ui.resolve(e)
    return h(Text, { color: 'warning' }, `⚠ signaller: ${band}`)
  })

  on('command.run', { command: 'signaller' }, async ($, e) => {
    const args = e.args.trim()
    if (/^name(\s|$)/i.test(args)) {
      try {
        const n = await rename($, args.slice(4).trim())
        const l = await lever($).catch(() => undefined)
        if (l) show($, l) // the status line picks the new name up now, not at the next turn
        return { text: n ? `this session is "${n}" in the console` : 'session name cleared' }
      } catch (err) {
        return { text: `signaller isn't reachable at ${BASE}: ${(err as Error).message}` }
      }
    }
    const run = /^run(?:\s+(\S+))?$/i.exec(args) // before the settings form: a downstream named "run" can't be set from here
    if (run) {
      let ps: Record<string, string>
      try {
        ps = (await lever($)).prompts ?? {}
      } catch (err) {
        return { text: `signaller isn't reachable at ${BASE}: ${(err as Error).message}` }
      }
      const name = run[1]?.toLowerCase()
      if (!name || !Object.hasOwn(ps, name)) {
        const list = Object.entries(ps).map(([n, t]) => `▸ /signaller run ${n}\n${t.replace(/^/gm, '    ')}`).join('\n\n')
        return { text: `${name ? `No prompt named "${name}". ` : ''}${list ? `Saved prompts:\n\n${list}` : 'No saved prompts yet: add them in the console under Prompts.'}` }
      }
      // into the prompt box, not submitted: the person reads it, can add to it, and sends it with Enter, so its
      // /signaller steps are theirs (a command can't submit a prompt anyway: it holds the turn)
      const { isFilled } = await $.prompt.fill({ text: ps[name]! })
      return { text: isFilled ? `"${name}" is in the prompt box: press Enter to send it` : ps[name]! }
    }
    const f = parseForm(args)
    if (f) return { text: (await setSettings($, f)).text }
    const want = args.toLowerCase()
    if (want && want !== 'reset' && !MODES.includes(want))
      return { text: `Unknown mode "${want}". Use one of: ${MODES.join(', ')}, reset, or name <text>.` }
    return { text: (await switchTo($, want)).text }
  })
}
