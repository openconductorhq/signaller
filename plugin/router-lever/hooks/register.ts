import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const LEVER = `${BASE}/ui/api/lever`
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

const label = (l: Lever) => (l.session ? `${l.lever} (this session)` : `${l.lever} (global)`)
const show = ($: EngineInterface, l?: Lever) => $.ui.status(l ? `router: ${label(l)}` : 'router: down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router',
      description: "Show or switch this session's claude-router mode; reset goes back to the global one",
      argumentHint: '[rules|valar|anthropic|local|reset]',
      immediate: true,
    })
    const poll = () => lever($).then(l => show($, l), () => show($))
    poll()
    $.clock.every(15_000, poll)
    return next(e)
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const want = e.args.trim().toLowerCase()
    if (want && want !== 'reset' && !MODES.includes(want))
      return { text: `Unknown mode "${want}". Use one of: ${MODES.join(', ')}, or reset.` }
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
