import type { EngineInterface, Register } from 'claude-code'

const BASE = 'http://127.0.0.1:8787'
const LEVER = `${BASE}/ui/api/lever`
const NAME = `${BASE}/ui/api/name`
const SETTINGS = `${BASE}/ui/api/settings`
const TRAIN = `${BASE}/ui/api/train`
let MODES = ['rules', 'auto', 'anthropic'] // until the router's list arrives: auto is a mode of the router itself
const HEADERS = { origin: BASE, 'content-type': 'application/json' } // the console takes writes from its own origin only

type Lever = { lever: string; session: string | null; global: string; modes?: string[]; name?: string | null; auto?: { phase: string; why: string } | null; prompts?: Record<string, string>; notice?: { turn: string; text: string } | null; trained?: boolean }
type TrainProject = { dir: string; name: string; sessions: number; prompts: number | null; usd: number | null }
type TrainJob = { projects: string[]; items: number; labelled: number; failed: number; remaining: number; usd_est: number; usd_remaining: number; usd_spent: number; cut: { items: number; usd: number } | null; cut_applied: boolean }
type TrainResult = { rules: { planning_kept: number; execution_moved: number }; model: { planning_kept: number; execution_moved: number }; planning: number; execution: number; verdict: string } | null
type TrainStatus = { state: string; projects: TrainProject[]; job: TrainJob | null; result: TrainResult; error: string | null }
type Line = { downstream: string; tier: string | null; set: string; value: string }
type Form = { ds: string; tier: string | null; key: 'window' | 'model'; value: string } | { ds: string; reset: true }

// every router call in one place: the fetch, the ok check and the JSON parse. A router answer (!ok) is an
// Answered error and reads as "signaller answered N: …"; only a fetch that never got an answer means down.
class Answered extends Error {
  constructor(readonly status: number, body: string) { super(`signaller answered ${status}: ${body.slice(0, 200)}`) }
}
const down = (err: unknown): string =>
  err instanceof Answered ? err.message : `signaller isn't reachable at ${BASE}: ${(err as Error).message}`
const call = async ($: EngineInterface, url: string, init?: { method: string; body: unknown }): Promise<any> => {
  const r = await $.http.fetch(url, init
    ? { ...init, headers: HEADERS, body: JSON.stringify(init.body) }
    : { headers: HEADERS })
  if (!r.ok) throw new Answered(r.status, r.text)
  return JSON.parse(r.text)
}

const fpOn = (v: string | undefined): boolean => !!v && !/^(0|false|no|off)$/i.test(v.trim())
// "&fp=1|0" for the lever read; nothing when the variable can't be read (the router keeps its last report)
const fpQ = async ($: EngineInterface): Promise<string> => {
  try { return `&fp=${fpOn(await $.env.get('_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL')) ? 1 : 0}` } catch { return '' }
}

// this session's lever: its own if /signaller set one, else the router's global one. The read also tells the
// router whether this Claude Code runs with the first-party flag (the console's signallercode hint).
const lever = async ($: EngineInterface, set?: { lever: string | null }): Promise<Lever> => {
  const session = await $.session.id()
  const l = await (set
    ? call($, LEVER, { method: 'PUT', body: { session, ...set } })
    : call($, `${LEVER}?session=${encodeURIComponent(session)}${await fpQ($)}`)) as Lever
  if (Array.isArray(l.modes) && l.modes.length) MODES = l.modes
  return l
}

// what the router console calls this session; empty clears it
const rename = async ($: EngineInterface, name: string): Promise<string | null> => {
  const session = await $.session.id()
  return (await call($, NAME, { method: 'PUT', body: { session, name } })).name
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
const downstreams = () => MODES.filter(m => m !== 'rules' && m !== 'auto' && m !== 'anthropic')

// --- teaching auto: the router builds and fits, the plugin labels with haiku and shows the result ---

const usd = (v: number) => `$${!v ? '0' : v < 0.1 ? v.toFixed(3) : v.toFixed(2)}`  // keep in step with ui/app.js's usd
const trainGet = async ($: EngineInterface): Promise<TrainStatus> => await call($, TRAIN)
const trainPost = async ($: EngineInterface, path: string, body: unknown): Promise<unknown> =>
  await call($, `${TRAIN}${path}`, { method: 'POST', body })
// the person's own definition, verbatim, plus the one-word rule; nothing a model reads ever says "/signaller".
// The first line is the marker the router routes on: these requests go to Anthropic untouched, whatever the lever.
const LABEL_SYSTEM = "signaller-label-v1\nanything which isn't brainstorming, speccing or planning is execution\nAnswer with one word: planning or execution."
// lenient parse: the lowercased reply must hold exactly one of the two words ("Planning." parses; both words,
// a sentence, or no answer at all is a failure, posted as failed and never stored as a label)
const labelOf = (text: string): 'planning' | 'execution' | null => {
  const t = text.toLowerCase()
  const p = t.includes('planning'), e = t.includes('execution')
  return p === e ? null : p ? 'planning' : 'execution'
}
const ask = async ($: EngineInterface, question: string, options: readonly string[], multi?: true): Promise<string> => {
  try {
    return await $.ui.ask(question, { options, header: 'train', ...(multi ? { multiSelect: true } : {}) })
  } catch {
    return '' // dismissed: as good as cancel
  }
}
// which projects: up to the 4 with the most prompts, multi-select; free text under Other names project dirs
const pickDirs = async ($: EngineInterface, ps: TrainProject[]): Promise<string[] | null> => {
  const top = [...ps].sort((a, b) => (b.prompts ?? 0) - (a.prompts ?? 0)).slice(0, 4)
  const option = (p: TrainProject) => `${p.name} · ${p.prompts} prompts · ${usd(p.usd ?? 0)}`
  const byOption = new Map(top.map(p => [option(p), p.dir]))  // option label -> dir, not re-rendered per part
  const answer = await ask($, 'Which projects should auto learn from?', [...byOption.keys()], true)
  if (!answer) return null
  const dirs: string[] = []
  for (const part of answer.split(/,\s*/)) {
    const d = byOption.get(part)
    if (d) dirs.push(d)
    else dirs.push(...part.split(/\s+/).filter(Boolean)) // Other: free text project dirs
  }
  return dirs.length ? dirs : null
}
// one loop at a time per session: the router's next() hands the same items to anyone who asks, so a second
// command while one runs would pay haiku twice over the same prompts. The flag is the loop's latest job block
// (for the progress answer) and clears when the loop ends, fails or is cancelled.
let training: TrainJob | null = null
// one confirm, then the loop runs unattended; the command returns at once with what started
const startTraining = async ($: EngineInterface, rest: string): Promise<string> => {
  if (training !== null) return `a training run is already labelling here: ${training.labelled} of ${training.items} prompts done; its result question comes at the end`
  const ps = (await trainGet($)).projects
  let dirs = rest ? rest.split(/\s+/).filter(Boolean) : null
  if (!dirs) {
    if (!ps.length) return 'no projects found yet; the console lists them once you have sessions'
    if (ps.some(p => p.prompts === null)) return 'the router is still scanning your projects — try again in a moment'
    dirs = await pickDirs($, ps)
    if (!dirs) return 'no projects picked'
  }
  let job = (await trainPost($, '/plan', { projects: dirs, cut: false })) as TrainJob
  if (!job.items) return `no unlabelled prompts in ${dirs.join(', ')}`
  training = job  // before the cost question: a second command while the person reads it answers with the progress
  if (job.remaining) {
    const options = ['Label all', ...(job.cut ? [`Newest only (about ${usd(job.cut.usd)})`] : []), 'Cancel']
    const answer = await ask($, `Label ${job.remaining} prompts (about ${usd(job.usd_remaining)})?`, options)
    if (answer !== 'Label all' && !answer.startsWith('Newest only')) { training = null; return 'cancelled' }  // cancel or dismiss
    if (answer.startsWith('Newest only')) {
      try { job = (await trainPost($, '/plan', { projects: dirs, cut: true })) as TrainJob }
      catch (err) { training = null; throw err }
    }
  }
  training = job
  trainLoop($).catch(err => $.ui.log(`⚠ signaller: training stopped: ${(err as Error).message}`))
  return job.remaining ? `labelling ${job.remaining} prompts…` : 'all these prompts are already labelled — fitting your model…'
}
// batches of 20, 4 at a time, each prompt to haiku on its own; then the router fits and evaluates
const trainLoop = async ($: EngineInterface): Promise<void> => {
  try {
    for (;;) {
      const { items } = (await trainPost($, '/next', { n: 20 })) as { items: { id: string; text: string }[] }
      if (!items?.length) break
      const labels: { id: string; label: string }[] = [], failed: string[] = []
      const usage = { input_tokens: 0, output_tokens: 0 }
      for (let i = 0; i < items.length; i += 4) {
        const chunk = items.slice(i, i + 4)
        const got = await Promise.all(chunk.map(async it => {
          try {
            const r = await $.model.complete({ model: 'haiku', system: LABEL_SYSTEM, prompt: it.text, maxTokens: 5 })
            usage.input_tokens += r.usage?.input_tokens ?? 0  // a reply without usage counts as 0
            usage.output_tokens += r.usage?.output_tokens ?? 0
            return r.isAnswered ? labelOf(r.text) : null
          } catch { return null }  // one failed call is one failed id, never a lost batch
        }))
        got.forEach((l, j) => (l ? labels.push({ id: chunk[j]!.id, label: l }) : failed.push(chunk[j]!.id)))
      }
      const job = (await trainPost($, '/labels', { labels, failed, usage })) as TrainJob
      training = job
      $.ui.status(`training auto · ${job.labelled}/${job.items}`)
    }
    await trainPost($, '/finish', {})
    let st: TrainStatus
    for (;;) { // the fit runs on the router's worker; poll until it lands
      st = await trainGet($)
      if (st.state === 'done' || st.state === 'failed') break
      await $.clock.sleep(2000)
    }
    if (st.state === 'failed') {
      $.ui.log(`⚠ signaller: training failed: ${st.error ?? 'unknown error'}`)
      return
    }
    const r = st.result
    $.ui.log(r?.verdict === 'too few'
      ? 'signaller: not enough planning prompts to judge your habits yet; label more with the same command'
      : `signaller: Your model keeps ${r!.model.planning_kept} of ${r!.planning} planning turns on Anthropic (rules alone: ${r!.rules.planning_kept}); moves ${r!.model.execution_moved} of ${r!.execution} execution turns.`)
    // like the card: "too few" offers no Use it at all — the router's /use would refuse it anyway
    const answer = await ask($, 'Use your model?', r?.verdict === 'too few' ? ['Keep rules only'] : ['Use it', 'Keep rules only'])
    if (answer === 'Use it') await trainPost($, '/use', {})
    else if (answer === 'Keep rules only') await trainPost($, '/discard', {})
    else $.ui.log('signaller: the console still holds the choice under Teach auto.')
  } finally {
    training = null
    $.ui.status(undefined)  // also on a throw: the "training auto" line never outlives the loop
  }
}

// Claude may switch the lever or change settings only on the person's say-so. The "/signaller <mode>" and
// settings forms a prompt carries become the queue of allowed steps, in the order typed: each call must match
// the next one and uses it up (a plan's "anthropic, rules, anthropic, rules" runs only in that order). A later
// prompt with no "/signaller" keeps the queue (messages sent while Claude works); one with any replaces it, so
// leftovers don't linger. Only the main agent spends the queue: subagents follow the session's lever anyway.
// Kept in the plugin's store under the session id, so a reload or a restart of the same session keeps it.
// ponytail: an ended session's leftover steps stay in the store (tiny, keyed by an id nobody reuses).
const qkey = async ($: EngineInterface) => `pending:${await $.session.id()}`
// take/giveBack/grant are async read-modify-writes on the store: every queue op is chained through one
// promise, so two calls in flight can't spend one grant twice or overwrite a newer prompt's queue.
let qtail: Promise<unknown> = Promise.resolve()
const serial = <T>(op: () => Promise<T>): Promise<T> => {
  const run = qtail.then(op)
  qtail = run.then(() => undefined, () => undefined)
  return run
}
const queue = async ($: EngineInterface) => (((await $.store.get(await qkey($))) as string[] | undefined) ?? [])
const save = async ($: EngineInterface, q: string[]) => {
  const k = await qkey($)
  await (q.length ? $.store.set(k, q) : $.store.delete(k))
}
// "/signaller run …", "/signaller name …" and "/signaller train …" are commands, not steps: quoting one in a
// prompt keeps the queue (run/name/train are exempt unless a word continues: "runner" or "trainer" are levers,
// "run," or "train (above)" aren't)
const GRANT = /(?:^|\s)\/signaller\s+(?!(?:run|name|train)(?![a-z0-9_-]))([a-z0-9_-]+)/gi
const GRANT_SETTINGS = new RegExp(`(?:^|\\s)\\/signaller\\s+${TAIL}`, 'gi') // the same form, as typed in a prompt
// "/signaller <downstream> reset…" with more than the word "reset" there is a settings attempt that grants nothing
const RESETISH = /(?:^|\s)\/signaller\s+[a-z0-9_-]+(?:\s+(?:fable|opus|sonnet|haiku))?\s+reset[a-z0-9_-]*/gi
const grant = ($: EngineInterface, text: string) => serial(async () => {
  const steps = new Map<number, string | null>() // keyed by where the "/signaller" starts; all three regexes share it
  for (const m of text.matchAll(GRANT_SETTINGS)) steps.set(m.index!, `set ${canon(form(m))}`)
  for (const m of text.matchAll(RESETISH)) if (!steps.has(m.index!)) steps.set(m.index!, null) // "resetting": an attempt, no grant
  for (const m of text.matchAll(GRANT)) if (!steps.has(m.index!)) steps.set(m.index!, `lever ${m[1]!.toLowerCase()}`) // modes are checked when used
  if (!steps.size) return
  await save($, [...steps].sort((a, b) => a[0] - b[0]).flatMap(([, s]) => (s ? [s] : [])))
})
const take = ($: EngineInterface, want: string) => serial(async () => {
  const q = await queue($)
  if (q[0] !== want) return false
  q.shift()
  await save($, q)
  return true
})
const giveBack = ($: EngineInterface, step: string) => serial(async () => {
  const q = await queue($)
  q.unshift(step)
  await save($, q)
})
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
    const back = await call($, SETTINGS, { method: 'PUT', body }) as { lines: Line[]; collisions: { text: string }[] }
    if ('reset' in f) return { ok: true, text: `this session's ${f.ds} settings cleared` }
    const lines = back.lines.map(l => `${l.downstream} ${l.set} ${l.value} for this session${l.tier ? ` (${l.tier})` : ''}`)
    return { ok: true, text: [...lines, ...back.collisions.map(x => x.text)].join('\n') }
  } catch (err) {
    if (typed !== undefined) await giveBack($, `set ${c}`)
    return { ok: false, text: down(err) }
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
    return { ok: false, text: down(err) }
  }
}

const phase = (l: Lever) => (l.lever === 'auto' && (l.auto?.phase === 'planning' || l.auto?.phase === 'execution') ? ` · ${l.auto.phase}` : '')
const label = (l: Lever) => `${l.lever}${phase(l)} (${l.session ? 'this session' : 'global'})${l.name ? `, session name: ${l.name}` : ''}`
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
// the band while auto runs on rules alone: at most the first 3 sessions ever seen on auto-untrained, off for
// everyone once dismissed ("/signaller train hide"), gone the moment a model is in use.
let hint: string | null = null
let hintUsd: number | null = null // the rough labelling cost, once a GET has said it; null while unknown
const HINT_KEY = 'trainHint'
const readHint = async ($: EngineInterface): Promise<{ sessions: string[]; hidden: boolean }> => {
  const v = (await $.store.get(HINT_KEY)) as { sessions?: unknown; hidden?: unknown } | undefined
  return { sessions: Array.isArray(v?.sessions) ? v!.sessions as string[] : [], hidden: v?.hidden === true }
}
const writeHint = ($: EngineInterface, s: { sessions: string[]; hidden: boolean }) => $.store.set(HINT_KEY, s)
// show: this session counts among the first 3 auto sessions ever seen, and the hint isn't hidden
const hintState = ($: EngineInterface): Promise<{ show: boolean }> => serial(async () => {
  const sid = await $.session.id()
  const s = await readHint($)
  if (!s.hidden && s.sessions.length < 3 && !s.sessions.includes(sid)) {
    s.sessions.push(sid)
    await writeHint($, s)
  }
  return { show: !s.hidden && s.sessions.includes(sid) }
})
const trainHint = async ($: EngineInterface, l: Lever) => {
  if (l.lever !== 'auto' || l.trained) {
    if (hint !== null) { hint = null; $.ui.invalidate('ui.render') }
    return
  }
  const show = (await hintState($)).show
  if (hintUsd === null) {
    try {
      const pr = (await trainGet($)).projects
      // mid-scan rows have no usd yet: caching their 0 would freeze an under-quote; a later poll tries again
      if (pr.every(p => p.usd !== null)) hintUsd = pr.reduce((a, p) => a + (p.usd ?? 0), 0)
    } catch { /* down: the band shows without the cost */ }
  }
  const text = show
    ? `auto runs on rules only · /signaller train teaches it your habits${hintUsd ? ` (about ${usd(hintUsd)})` : ''} · /signaller train hide`
    : null
  if (text !== hint) { hint = text; $.ui.invalidate('ui.render') }
}
const hideHint = async ($: EngineInterface) => {
  await serial(async () => { await writeHint($, { ...(await readHint($)), hidden: true }) })
  if (hint !== null) { hint = null; $.ui.invalidate('ui.render') }
}
const poll = ($: EngineInterface) => { lever($).then(l => (show($, l), warn($, l), trainHint($, l).catch(() => undefined)), () => show($)) }
const show = ($: EngineInterface, l?: Lever) => $.ui.status(l ? label(l) : 'down')

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({
      name: 'signaller',
      description:
        "Show or switch this session's signaller mode; reset goes back to the global one; " +
        '<downstream> [<tier>] window|model <value> sets this session’s settings line, <downstream> reset clears them; name <text> labels it in the console; ' +
        'train [projects|hide] teaches auto your habits (labels your prompts with Haiku, asks once first; hide drops the hint band)',
      argumentHint: '[mode|reset|name <text>|train [projects|hide]|<downstream> [tier] window|model <value>]',
      immediate: true,
    })
    // the same switch for Claude itself: mcp__signaller__signaller_lever
    await $.tool.register({
      name: 'signaller_lever',
      description:
        "Show or switch signaller's lever for this Claude Code session (its subagents follow). " +
        'lever: rules (follow the rulebook), auto (the router decides per turn: planning or execution), anthropic, or a downstream id (send everything there), reset (back to the global lever); omit it to only read the current one. ' +
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

  // the pending image fallback above the prompt, or the untrained-auto hint; a survey owns the band, and the
  // fallback wins when both apply
  on('ui.render', { component: 'AbovePrompt' }, ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const text = band ?? hint
    if (text === null) return next(e)
    const { Text } = $.ui.resolve(e)
    return h(Text, { color: 'warning' }, `⚠ signaller: ${text}`)
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
        return { text: down(err) }
      }
    }
    const run = /^run(?:\s+(\S+))?$/i.exec(args) // before the settings form: a downstream named "run" can't be set from here
    if (run) {
      let ps: Record<string, string>
      try {
        ps = (await lever($)).prompts ?? {}
      } catch (err) {
        return { text: down(err) }
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
    const train = /^train(\s|$)/i.exec(args) // before the settings form, like run
    if (train) {
      const rest = args.slice(5).trim()
      try {
        if (/^hide(\s|$)/i.test(rest)) {
          await hideHint($)
          return { text: 'the training hint is off' }
        }
        return { text: await startTraining($, rest) }
      } catch (err) {
        return { text: down(err) }
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
