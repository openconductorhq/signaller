import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const LEVER = `${BASE}/ui/api/lever`
const NAME = `${BASE}/ui/api/name`
const MODES = ['rules', 'valar', 'anthropic', 'local']
const HEADERS = { origin: BASE, 'content-type': 'application/json' } // the console takes writes from its own origin only

type Lever = { lever: string; session: string | null; global: string }

// this session's lever: its own if /router set one, else the router's global one
const lever = async ($: EngineInterface, set?: { lever: string | null }): Promise<Lever> => {
  const session = await $.session.id()
  const r = set
    ? await $.http.fetch(LEVER, { method: 'PUT', headers: HEADERS, body: JSON.stringify({ session, ...set }) })
    : await $.http.fetch(`${LEVER}?session=${encodeURIComponent(session)}`)
  if (!r.ok) throw new Error(`the router answered ${r.status}: ${r.text.slice(0, 200)}`)
  return JSON.parse(r.text)
}

// what the router console calls this session; empty clears it
const rename = async ($: EngineInterface, name: string): Promise<string | null> => {
  const session = await $.session.id()
  const r = await $.http.fetch(NAME, { method: 'PUT', headers: HEADERS, body: JSON.stringify({ session, name }) })
  if (!r.ok) throw new Error(`the router answered ${r.status}: ${r.text.slice(0, 200)}`)
  return JSON.parse(r.text).name
}

// Claude may switch the lever only on the person's say-so: each "/router <mode>" they type, in any
// prompt (e.g. a multi-step plan), allows Claude one switch to that mode. Later prompts don't erase it.
// Kept in memory; a reload forgets them, which only denies.
const granted: string[] = []
const GRANT = /(?:^|\s)\/router\s+([a-z]+)\b/gi
const grant = (text: string) => {
  for (const m of text.matchAll(GRANT)) {
    const mode = m[1]!.toLowerCase()
    if (mode === 'reset' || MODES.includes(mode)) granted.push(mode)
  }
}
const use = (want: string) => {
  const i = granted.indexOf(want)
  return i >= 0 && granted.splice(i, 1).length > 0
}

const label = (l: Lever) => (l.session ? `${l.lever} (this session)` : `${l.lever} (global)`)
const show = ($: EngineInterface, l?: Lever) => $.ui.status(l ? `router: ${label(l)}` : 'router: down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router',
      description: "Show or switch this session's claude-router mode; reset goes back to the global one; name <text> labels it in the console",
      argumentHint: '[rules|valar|anthropic|local|reset|name <text>]',
      immediate: true,
    })
    // the same switch for Claude itself: mcp__router-lever__router_lever
    await $.tool.register({
      name: 'router_lever',
      description:
        "Show or switch claude-router's lever for this Claude Code session (its subagents follow). " +
        'lever: rules (follow the rulebook), anthropic, valar or local (send everything there), reset (back to the global lever); omit it to only read the current one. ' +
        'Never switch on your own initiative: only when the person tells you to with "/router <mode>" in a message (e.g. "do /router anthropic", or a plan with "/router rules" at a later step). ' +
        'Each "/router <mode>" they typed allows one switch to that mode; others are refused.',
      inputSchema: {
        type: 'object',
        properties: { lever: { type: 'string', enum: [...MODES, 'reset'] } },
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
    const want = (e as { lever?: string }).lever
    if (want && want !== 'reset' && !MODES.includes(want)) return { deny: `Unknown lever "${want}". Use one of: ${MODES.join(', ')}, or reset.` }
    if (want && !use(want))
      return { deny: `Not switching: the person hasn't typed "/router ${want}" for this switch (each one they type allows one). Only switch when they tell you to that way; they can also type /router ${want} themselves.` }
    try {
      const l = await lever($, want ? { lever: want === 'reset' ? null : want } : undefined)
      show($, l)
      return { result: `claude-router: ${label(l)}` }
    } catch (err) {
      show($)
      return { deny: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
    }
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
    const want = args.toLowerCase()
    if (want && want !== 'reset' && !MODES.includes(want))
      return { text: `Unknown mode "${want}". Use one of: ${MODES.join(', ')}, reset, or name <text>.` }
    try {
      const l = await lever($, want ? { lever: want === 'reset' ? null : want } : undefined)
      show($, l)
      return { text: `claude-router: ${label(l)}` }
    } catch (err) {
      show($)
      return { text: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
    }
  })
}
