import { expect, test } from 'claude-code/testing'

const TOOL = 'mcp__router-lever__router_lever'

test('Claude switches the lever only when the latest prompt asks for it', async ($, on) => {
  const puts: string[] = []
  on('command.register', () => ({ value: { command: 'router' } }))
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('session.id', () => ({ value: 's1' }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('http.fetch', (_, e) => {
    if (e.init?.method === 'PUT') puts.push(String(e.init.body))
    return { value: { ok: true, status: 200, text: JSON.stringify({ lever: 'valar', session: 'valar', global: 'rules', modes: ['rules', 'valar', 'anthropic'] }) } }
  })
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: 'engine' }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

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

test('modes come from the router: a downstream id works, a removed one is unknown', async ($, on) => {
  on('command.register', () => ({ value: { command: 'router' } }))
  on('tool.register', () => ({ value: { tool: TOOL } }))
  on('session.id', () => ({ value: 's1' }))
  on('clock.every', () => ({ value: undefined }))
  on('ui.status', () => ({ value: undefined }))
  on('http.fetch', () => ({ value: { ok: true, status: 200, text: JSON.stringify({ lever: 'lemmi', session: 'lemmi', global: 'rules', modes: ['rules', 'anthropic', 'lemmi'] }) } }))
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: 'engine' }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  await $.prompt.submit({ text: 'do /router lemmi' })
  expect((await $.tool.call({ tool: TOOL, lever: 'lemmi' })).deny).toBeUndefined()
  expect((await $.tool.call({ tool: TOOL, lever: 'local' })).deny).toContain('Unknown lever')
})
