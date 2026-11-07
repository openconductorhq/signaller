import { expect, test } from 'claude-code/testing'

const TOOL = 'mcp__signaller__signaller_lever'
const LEVER = JSON.stringify({ lever: 'valar', session: 'valar', global: 'rules', modes: ['rules', 'valar', 'anthropic'] })

// one stub router per test: everything the hooks need, with the test's own http.fetch
const stub = ($: any, on: any, http: (e: any) => any, store = new Map<string, unknown>()) => {
  on('store.get', (_, e) => ({ value: store.get(e.key) }))
  on('store.set', (_, e) => (store.set(e.key, e.value), { value: undefined }))
  on('store.delete', (_, e) => (store.delete(e.key), { value: undefined }))
  on('command.register', () => ({ value: { command: 'signaller' } }))
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('session.id', () => ({ value: 's1' }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('http.fetch', (_, e) => http(e))
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: 'engine' }))
  return $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })
}

// first in the file on purpose: MODES is the module default until a lever answer refreshes it, and this test
// must see that default. The poll's GET fails (the router down at start), so nothing refreshes MODES first.
test("a typed '/signaller auto' is known before the router has answered once", async ($, on) => {
  await stub($, on, (e) => e.init?.method === 'PUT'
    ? { value: { ok: true, status: 200, text: JSON.stringify({ lever: 'auto', session: 'auto', global: 'rules', modes: ['rules', 'auto', 'anthropic'] }) } }
    : { value: { ok: false, status: 503, text: 'down' } })
  await $.prompt.submit({ text: 'do /signaller auto' })
  const r = await $.tool.call({ tool: TOOL, lever: 'auto' })
  expect(r.deny).toBeUndefined()
  expect(String(r.result)).toContain('auto')
})

test('Claude switches the lever only when the latest prompt asks for it', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(String(e.init.body))
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'please simplify this file' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // never asked
  expect((await $.tool.call({ tool: TOOL })).deny).toBeUndefined()                               // reading is fine

  await $.prompt.submit({ text: "switch the router to anthropic, don't use valar" })  // no "/signaller <mode>"
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')

  // a multi-step plan: each /signaller allows one switch, and a later prompt doesn't erase them
  await $.prompt.submit({ text: 'step 1: /signaller anthropic and /simplify. step 3: /signaller rules, then re-review' })
  await $.prompt.submit({ text: 'looks good, go on' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // never typed
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')  // used up
  expect((await $.tool.call({ tool: TOOL, lever: 'rules' })).deny).toBeUndefined()
  expect(puts).toEqual([JSON.stringify({ session: 's1', lever: 'anthropic' }), JSON.stringify({ session: 's1', lever: 'rules' })])
})

test('the settings form is gated like the lever: one write per typed /signaller, of exactly those values', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    const body = e.init?.method === 'PUT' ? String(e.init.body) : ''
    if (body.includes('"downstream"')) {
      puts.push(body)
      const b = JSON.parse(body)
      return { value: { ok: true, status: 200, text: JSON.stringify({
        lines: b.reset ? [] : [{ when: '', downstream: b.downstream, tier: b.tier ?? null, set: b.set, value: b.value }],
        collisions: [{ line: 2, by: 1, text: 'line 2 overlaps line 1 (line 1 wins where both match)' }],
      }) } }
    }
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'please leave the window alone' })
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')  // never asked

  await $.prompt.submit({ text: 'do /signaller valar window priority' })
  const ok = await $.tool.call({ tool: TOOL, settings: 'valar window priority' })
  expect(ok.deny).toBeUndefined()
  expect(ok.result).toContain('valar window priority for this session')
  expect(ok.result).toContain('line 2 overlaps line 1 (line 1 wins where both match)')
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')  // used up

  await $.prompt.submit({ text: 'do /signaller valar window standard' })  // a different value grants only itself
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')

  await $.prompt.submit({ text: 'do /signaller valar opus window priority' })  // the tier is part of the grant
  const tier = await $.tool.call({ tool: TOOL, settings: 'valar opus window priority' })
  expect(tier.deny).toBeUndefined()
  expect(tier.result).toContain('for this session (opus)')
  expect(JSON.parse(puts[puts.length - 1]!).tier).toBe('opus')
})

test('a typed settings form is no lever grant; the value keeps its case on its way to the router', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    const body = e.init?.method === 'PUT' ? String(e.init.body) : ''
    if (body.includes('"downstream"')) {
      puts.push(body)
      return { value: { ok: true, status: 200, text: JSON.stringify({ lines: [], collisions: [] }) } }
    }
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'do /signaller valar window priority' })  // a settings form grants only that write
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')

  await $.prompt.submit({ text: 'do /signaller valar' })  // one word: still the lever switch
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toBeUndefined()

  await $.prompt.submit({ text: 'do /signaller reset' })  // one word reset: still the lever reset
  expect((await $.tool.call({ tool: TOOL, lever: 'reset' })).deny).toBeUndefined()

  await $.prompt.submit({ text: 'do /signaller valar model Glm-5.4' })  // the value's case survives
  const ok = await $.tool.call({ tool: TOOL, settings: 'valar model Glm-5.4' })
  expect(ok.deny).toBeUndefined()
  expect(JSON.parse(puts[puts.length - 1]!).value).toBe('Glm-5.4')

  await $.prompt.submit({ text: 'do /signaller valar reset' })  // reset through the gate
  const cleared = await $.tool.call({ tool: TOOL, settings: 'valar reset' })
  expect(cleared.deny).toBeUndefined()
  expect(cleared.result).toContain("this session's valar settings cleared")

  expect((await $.tool.call({ tool: TOOL, settings: 'lemmi window priority' })).deny).toContain('Unknown downstream')
  expect(puts.every(p => !p.includes('lemmi'))).toBe(true)  // unknown downstream: no request
})

test('modes come from the router: a downstream id works, a removed one is unknown', async ($, on) => {
  await stub($, on, () => ({ value: { ok: true, status: 200, text: JSON.stringify(
    { lever: 'lemmi', session: 'lemmi', global: 'rules', modes: ['rules', 'anthropic', 'lemmi'] }) } }))

  await $.prompt.submit({ text: 'do /signaller lemmi' })
  expect((await $.tool.call({ tool: TOOL, lever: 'lemmi' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'local' })).deny).toContain('Unknown lever')
})

test('a settings value stops before trailing punctuation, and reset is a whole word', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    const body = e.init?.method === 'PUT' ? String(e.init.body) : ''
    if (body.includes('"downstream"')) {
      puts.push(body)
      return { value: { ok: true, status: 200, text: JSON.stringify({ lines: [], collisions: [] }) } }
    }
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'please /signaller valar model glm-5.4.' })
  const ok = await $.tool.call({ tool: TOOL, settings: 'valar model glm-5.4' })
  expect(ok.deny).toBeUndefined()
  expect(JSON.parse(puts[puts.length - 1]!).value).toBe('glm-5.4')  // the sentence's "." is not part of the value

  await $.prompt.submit({ text: '/signaller valar resetting' })  // "resetting" is not "reset": nothing is granted
  expect((await $.tool.call({ tool: TOOL, settings: 'valar reset' })).deny).toContain('Not setting')
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')
})

test('an image fallback shows once, as a transcript line and a band above the prompt', async ($, on) => {
  const logs: string[] = [], toasts: string[] = []
  let notice: any = null
  on('ui.log', (_, e) => { logs.push(e.text); return { value: undefined } })
  on('ui.toast', (_, e) => { toasts.push(e.text); return { value: undefined } })
  on('ui.render', () => h(Fragment, {}))  // what the engine draws beneath: the band's pass-through
  on('turn.complete', () => ({ text: 'answer' }))
  await stub($, on, () => ({ value: { ok: true, status: 200, text: JSON.stringify({ ...JSON.parse(LEVER), notice }) } }))
  const settle = () => new Promise(r => setTimeout(r, 20))  // polls run in the background
  const BAND = {
    plugin: 'signaller', surface: 'terminal' as const, component: 'AbovePrompt' as const,
    props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 80, scroll: { offset: 0, bodyRows: 10 }, view: {} },
  }
  const ui = await $.ui.mount(BAND)
  await settle()  // the first poll only takes note
  const quiet = JSON.stringify(await ui.drawn())  // nothing pending: the band draws nothing of signaller's
  notice = { turn: 't1', text: 'This turn went to Anthropic, not valar.' }
  await $.turn.complete({ reason: 'answer' })
  await settle()
  expect(logs).toEqual(['⚠ signaller: This turn went to Anthropic, not valar.'])
  expect(toasts).toEqual([])  // no toast anymore
  expect(JSON.stringify(await ui.drawn())).toContain('⚠ signaller: This turn went to Anthropic, not valar.')
  await ui.redraw({ ...BAND.props, hasSurvey: true })  // a survey holds the band: yield to it
  expect(JSON.stringify(await ui.drawn())).toBe(quiet)
  await ui.redraw(BAND.props)  // the survey is gone
  await $.turn.complete({ reason: 'answer' })  // the same turn's notice again: not repeated
  await settle()
  expect(logs).toEqual(['⚠ signaller: This turn went to Anthropic, not valar.'])  // the line, once
  expect(JSON.stringify(await ui.drawn())).toContain('⚠ signaller: ')
  await $.prompt.submit({ text: 'next' })
  expect(JSON.stringify(await ui.drawn())).toBe(quiet)  // the next prompt clears it
  await ui.unmount()
})

test('a typed plan runs in order, survives messages sent meanwhile, and only the main agent spends it', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(JSON.parse(String(e.init.body)).lever)
    return { value: { ok: true, status: 200, text: LEVER } }
  })
  const sw = async (lever: string, agentId?: string) => (await $.tool.call({ tool: TOOL, lever, ...(agentId ? { agentId } : {}) })).deny

  await $.prompt.submit({ text: 'do /signaller valar' })  // a leftover the plan below replaces
  await $.prompt.submit({ text: 'once done, run:\n  1. /signaller anthropic\n  2. /simplify\n  3. /signaller rules\n  5. /signaller anthropic\n  7. /signaller rules' })
  expect(await sw('valar')).toContain('Not switching')            // the leftover is gone
  expect(await sw('rules')).toContain('"/signaller anthropic"')   // out of order: names the next step
  expect(await sw('anthropic')).toBeUndefined()
  await $.prompt.submit({ text: 'also check the README while you are at it' })  // a message mid-plan keeps the queue
  expect(await sw('rules', 'sub-1')).toContain('only the main agent')  // a subagent can't spend it
  expect(await sw('rules')).toBeUndefined()
  expect(await sw('anthropic')).toBeUndefined()
  expect(await sw('rules')).toBeUndefined()
  expect(await sw('rules')).toContain('Not switching')  // used up
  expect(puts).toEqual(['anthropic', 'rules', 'anthropic', 'rules'])
})

test('pasting a refusal back grants nothing', async ($, on) => {
  await stub($, on, () => ({ value: { ok: true, status: 200, text: LEVER } }))
  const deny = (await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny!
  expect(deny).toContain('Not switching')
  await $.prompt.submit({ text: `how come? ${deny}` })
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')
})

test('/signaller run fills the prompt box with a saved prompt; sent, its /signaller steps are queued', async ($, on) => {
  const filled: string[] = []
  const PLAN = 'execute it.\n  1. /signaller anthropic\n  2. /simplify\n  3. /signaller rules'
  on('prompt.fill', (_, e) => { filled.push(e.text); return { isFilled: true } })
  await stub($, on, () => ({ value: { ok: true, status: 200, text: JSON.stringify({ ...JSON.parse(LEVER), prompts: { execute: PLAN } }) } }))
  const list = (await $.command.run({ command: 'signaller', args: 'run' })).text!
  expect(list).toContain('▸ /signaller run execute')
  expect(list).toContain('    1. /signaller anthropic')  // the content, indented under its name
  expect((await $.command.run({ command: 'signaller', args: 'run nope' })).text).toContain('No prompt named "nope"')
  expect((await $.command.run({ command: 'signaller', args: 'run execute' })).text).toContain('press Enter')
  expect(filled).toEqual([PLAN])
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')  // not sent yet
  await $.prompt.submit({ text: filled[0]! })  // the person presses Enter
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'rules' })).deny).toBeUndefined()
})

test('typed steps outlive a restart, and quoting /signaller run or name keeps them', async ($, on) => {
  const store = new Map<string, unknown>([['pending:s1', ['lever anthropic', 'lever rules']]])  // saved before a restart
  await stub($, on, () => ({ value: { ok: true, status: 200, text: LEVER } }), store)

  await $.prompt.submit({ text: 'status? I sent /signaller run execute and /signaller name presets earlier' })
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect(store.get('pending:s1')).toEqual(['lever rules'])
  expect((await $.tool.call({ tool: TOOL, lever: 'rules' })).deny).toBeUndefined()
  expect(store.has('pending:s1')).toBe(false)  // used up: nothing left behind
})

test('quoting /signaller run or name keeps them, whatever follows the word', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(JSON.parse(String(e.init.body)).lever)
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'do /signaller anthropic' })
  // punctuation after run/name — not a word continuing it: no step granted, the queue stands
  await $.prompt.submit({ text: 'see /signaller run, and also /signaller name (as typed above)' })
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')  // used up
  expect(puts).toEqual(['anthropic'])  // run/name granted nothing, no other switch went out

  // a word that continues ("runner") is a lever like any other, not the run command
  await $.prompt.submit({ text: 'do /signaller runner' })
  expect((await $.tool.call({ tool: TOOL, lever: 'runner' })).deny).toContain('Unknown lever')
})

test('two calls at once spend one typed grant once', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(JSON.parse(String(e.init.body)).lever)
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'do /signaller valar' })
  const [a, b] = await Promise.all([  // parallel tool calls: one read-modify-write each, unserialized would race
    $.tool.call({ tool: TOOL, lever: 'valar' }),
    $.tool.call({ tool: TOOL, lever: 'valar' }),
  ])
  expect([a, b].some(x => x.deny === undefined)).toBe(true)  // exactly one switched
  expect([a, b].some(x => (x.deny ?? '').includes('Not switching'))).toBe(true)  // the other was refused
  expect(puts).toEqual(['valar'])  // one grant, one switch on the wire
})

test('auto is a mode, not a downstream: one typed step, the phase in the status, no settings form', async ($, on) => {
  const AUTO = { lever: 'auto', session: 'auto', global: 'rules', modes: ['rules', 'auto', 'valar', 'anthropic'], auto: { phase: 'planning', why: 'x' } }
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(String(e.init.body))
    return { value: { ok: true, status: 200, text: JSON.stringify(AUTO) } }
  })

  await $.prompt.submit({ text: 'do /signaller auto' })
  expect((await $.tool.call({ tool: TOOL, lever: 'auto' })).deny).toBeUndefined()
  expect(puts).toEqual([JSON.stringify({ session: 's1', lever: 'auto' })])
  expect((await $.tool.call({ tool: TOOL, lever: 'auto' })).deny).toContain('Not switching')  // used up
  expect((await $.tool.call({ tool: TOOL })).result).toBe('auto · planning (this session)')

  await $.prompt.submit({ text: 'do /signaller auto window now' })
  expect((await $.tool.call({ tool: TOOL, settings: 'auto window now' })).deny).toContain('Unknown downstream')
  expect(puts.length).toBe(1)
})
