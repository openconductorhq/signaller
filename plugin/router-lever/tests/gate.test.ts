import { expect, test } from 'claude-code/testing'

const TOOL = 'mcp__router-lever__router_lever'
const LEVER = JSON.stringify({ lever: 'valar', session: 'valar', global: 'rules', modes: ['rules', 'valar', 'anthropic'] })

// one stub router per test: everything the hooks need, with the test's own http.fetch
const stub = ($: any, on: any, http: (e: any) => any) => {
  on('command.register', () => ({ value: { command: 'router' } }))
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

test('Claude switches the lever only when the latest prompt asks for it', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    if (e.init?.method === 'PUT') puts.push(String(e.init.body))
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'please simplify this file' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // never asked
  expect((await $.tool.call({ tool: TOOL })).deny).toBeUndefined()                               // reading is fine

  await $.prompt.submit({ text: "switch the router to anthropic, don't use valar" })  // no "/router <mode>"
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')

  // a multi-step plan: each /router allows one switch, and a later prompt doesn't erase them
  await $.prompt.submit({ text: 'step 1: /router anthropic and /simplify. step 3: /router rules, then re-review' })
  await $.prompt.submit({ text: 'looks good, go on' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // never typed
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')  // used up
  expect((await $.tool.call({ tool: TOOL, lever: 'rules' })).deny).toBeUndefined()
  expect(puts).toEqual([JSON.stringify({ session: 's1', lever: 'anthropic' }), JSON.stringify({ session: 's1', lever: 'rules' })])
})

test('the settings form is gated like the lever: one write per typed /router, of exactly those values', async ($, on) => {
  const puts: string[] = []
  await stub($, on, (e) => {
    const body = e.init?.method === 'PUT' ? String(e.init.body) : ''
    if (body.includes('"downstream"')) {
      puts.push(body)
      const b = JSON.parse(body)
      return { value: { ok: true, status: 200, text: JSON.stringify({
        lines: b.reset ? [] : [{ when: '', downstream: b.downstream, tier: b.tier ?? null, set: b.set, value: b.value }],
        collisions: [{ line: 2, by: 1, text: 'line 2 is shadowed by line 1' }],
      }) } }
    }
    return { value: { ok: true, status: 200, text: LEVER } }
  })

  await $.prompt.submit({ text: 'please leave the window alone' })
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')  // never asked

  await $.prompt.submit({ text: 'do /router valar window priority' })
  const ok = await $.tool.call({ tool: TOOL, settings: 'valar window priority' })
  expect(ok.deny).toBeUndefined()
  expect(ok.result).toContain('claude-router: valar window priority for this session')
  expect(ok.result).toContain('line 2 is shadowed by line 1')
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')  // used up

  await $.prompt.submit({ text: 'do /router valar window standard' })  // a different value grants only itself
  expect((await $.tool.call({ tool: TOOL, settings: 'valar window priority' })).deny).toContain('Not setting')

  await $.prompt.submit({ text: 'do /router valar opus window priority' })  // the tier is part of the grant
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

  await $.prompt.submit({ text: 'do /router valar window priority' })  // a settings form grants only that write
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')

  await $.prompt.submit({ text: 'do /router valar' })  // one word: still the lever switch
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toBeUndefined()

  await $.prompt.submit({ text: 'do /router reset' })  // one word reset: still the lever reset
  expect((await $.tool.call({ tool: TOOL, lever: 'reset' })).deny).toBeUndefined()

  await $.prompt.submit({ text: 'do /router valar model Glm-5.4' })  // the value's case survives
  const ok = await $.tool.call({ tool: TOOL, settings: 'valar model Glm-5.4' })
  expect(ok.deny).toBeUndefined()
  expect(JSON.parse(puts[puts.length - 1]!).value).toBe('Glm-5.4')

  await $.prompt.submit({ text: 'do /router valar reset' })  // reset through the gate
  const cleared = await $.tool.call({ tool: TOOL, settings: 'valar reset' })
  expect(cleared.deny).toBeUndefined()
  expect(cleared.result).toContain("this session's valar settings cleared")

  expect((await $.tool.call({ tool: TOOL, settings: 'lemmi window priority' })).deny).toContain('Unknown downstream')
  expect(puts.every(p => !p.includes('lemmi'))).toBe(true)  // unknown downstream: no request
})

test('modes come from the router: a downstream id works, a removed one is unknown', async ($, on) => {
  await stub($, on, () => ({ value: { ok: true, status: 200, text: JSON.stringify(
    { lever: 'lemmi', session: 'lemmi', global: 'rules', modes: ['rules', 'anthropic', 'lemmi'] }) } }))

  await $.prompt.submit({ text: 'do /router lemmi' })
  expect((await $.tool.call({ tool: TOOL, lever: 'lemmi' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'local' })).deny).toContain('Unknown lever')
})
