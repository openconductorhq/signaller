import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const API = `${BASE}/ui/api`
const MODES = ['rules', 'valar', 'anthropic', 'local']
const HEADERS = { origin: BASE, 'content-type': 'application/json' } // the console takes writes from its own origin only

const config = async ($: EngineInterface) => {
  const r = await $.http.fetch(`${API}/state`)
  if (!r.ok) throw new Error(`the router answered ${r.status}`)
  return JSON.parse(r.text).config
}

const show = ($: EngineInterface, lever?: string) => $.ui.status(lever ? `router: ${lever}` : 'router: down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'router',
      description: 'Show or switch claude-router: rules, valar, anthropic or local',
      argumentHint: '[rules|valar|anthropic|local]',
      immediate: true,
    })
    const poll = () => config($).then(c => show($, c.lever), () => show($))
    poll()
    $.clock.every(15_000, poll)
    return next(e)
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const want = e.args.trim().toLowerCase()
    if (want && !MODES.includes(want)) return { text: `Unknown mode "${want}". Use one of: ${MODES.join(', ')}.` }
    let c
    try {
      c = await config($)
    } catch (err) {
      show($)
      return { text: `claude-router isn't reachable at ${BASE}: ${(err as Error).message}` }
    }
    if (!want || want === c.lever) {
      show($, c.lever)
      return { text: `claude-router: ${c.lever}` }
    }
    const r = await $.http.fetch(`${API}/config`, {
      method: 'PUT', headers: HEADERS, body: JSON.stringify({ ...c, lever: want }),
    })
    if (!r.ok) return { text: `claude-router refused the switch (${r.status}): ${r.text.slice(0, 200)}` }
    show($, want)
    return { text: `claude-router: ${c.lever} → ${want}` }
  })
}
