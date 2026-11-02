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
    return { value: { ok: true, status: 200, text: JSON.stringify({ lever: 'valar', session: 'valar', global: 'rules' }) } }
  })
  on('prompt.submit', (_, e) => ({ text: e.text }))
  on('session.start', (_, e) => ({ cwd: e.cwd }))
  on('tool.call', () => ({ result: 'engine' }))
  await $.session.start({ cwd: '/', surface: 'terminal', isInteractive: true })

  await $.prompt.submit({ text: 'please simplify this file' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // never asked
  expect((await $.tool.call({ tool: TOOL })).deny).toBeUndefined()               // reading is fine

  await $.prompt.submit({ text: "switch the router to anthropic, don't use valar" })  // no "/router <mode>"
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toContain('Not switching')

  await $.prompt.submit({ text: 'do /router anthropic then /simplify' })
  expect((await $.tool.call({ tool: TOOL, lever: 'valar' })).deny).toContain('Not switching')  // asked for a different mode
  expect((await $.tool.call({ tool: TOOL, lever: 'anthropic' })).deny).toBeUndefined()
  expect(puts).toEqual([JSON.stringify({ session: 's1', lever: 'anthropic' })])
})
