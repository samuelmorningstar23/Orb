/**
 * Demo mode: the real front end with a recorded backend.
 *
 * The website (orbsuite.com) embeds the demo build of this app. There is no
 * server behind it: this module patches `fetch` so every /api call is answered
 * from a recording of the real backend (docs/website-assets/capture.cjs
 * --record wrote one file per persona while driving the product on seeded demo
 * patients), and it hands the recorded realtime frames to the fake socket.
 *
 * Replay rules, kept deliberately simple:
 *   - key = METHOD + path + query. A key recorded several times is replayed in
 *     recording order, so state moves the way it did in the session (a note
 *     goes from draft to signed, the pharmacist queue fills).
 *   - if the exact key was never recorded, fall back to the same path without
 *     its query, then to the same path with any id-like segment wildcarded.
 *   - streaming (text/event-stream) bodies are replayed event by event with a
 *     short delay, so Sage still "thinks" and the note still "types".
 *   - anything else answers 404 with a JSON detail, which the app shows as its
 *     normal empty or error state. It is logged so the recording can be extended.
 *
 * Boot: the persona comes from ?persona= (doctor, surgeon, nurse, pharmacist,
 * admin, patient) and the tab from ?tab= (a nav id, e.g. vigil). The recorded
 * login response seeds localStorage so the app opens straight on the dashboard.
 */
import { io } from 'socket.io-client'
import { scriptById, type Action, type DemoScript } from './scripts'
import { news2 } from './news2'

interface Recorded { t: number; method: string; url: string; status: number; ct: string; body: string; req?: string }
interface Fixture { persona: string; recorded_at?: number; user: Record<string, unknown> | null; responses: Recorded[]; frames: { t: number; payload: string }[] }

const params = new URLSearchParams(window.location.search)
const persona = params.get('persona') || 'doctor'
const tab = params.get('tab') || ''
const subtab = params.get('subtab') || ''
const open = params.get('open') || ''
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

const sequences = new Map<string, Recorded[]>()
const cursors = new Map<string, number>()
const byPath = new Map<string, Recorded[]>()
let loaded = false
const unmatched = new Set<string>()

const norm = (u: string) => {
  const url = new URL(u, window.location.origin)
  return { path: url.pathname.replace(/^\/demo/, ''), query: url.search }
}

function index(fx: Fixture) {
  for (const r of fx.responses) {
    // A streamed answer the recorder could not read back is worse than none: skip it.
    if (r.status === 200 && !r.body) continue
    const { path, query } = norm(r.url)
    const key = `${r.method} ${path}${query}`
    if (!sequences.has(key)) sequences.set(key, [])
    sequences.get(key)!.push(r)
    const pkey = `${r.method} ${path}`
    if (!byPath.has(pkey)) byPath.set(pkey, [])
    byPath.get(pkey)!.push(r)
  }
}

/**
 * GET: the last recorded state (the richest: the queue after the note was
 * signed, the vitals at the end of the session). Mutations: recording order,
 * so a sign that first needed a typed SIGN still needs it. When several
 * mutations were recorded for one key, the one whose request body is closest
 * to this request wins (the simulator preset you clicked, the question you asked).
 */
function pick(list: Recorded[], key: string, method = 'GET', reqBody = ''): Recorded {
  if (method === 'GET') return list[list.length - 1]
  if (reqBody && list.length > 1 && list.some(r => r.req)) {
    const want = tokens(reqBody)
    let best = list[0], bestScore = -1
    for (const r of list) {
      const have = tokens(r.req || '')
      let score = 0
      for (const w of want) if (have.has(w)) score++
      if (score > bestScore) { best = r; bestScore = score }
    }
    return best
  }
  const i = cursors.get(key) ?? 0
  const r = list[Math.min(i, list.length - 1)]
  cursors.set(key, i + 1)
  return r
}

const tokens = (s: string): Set<string> => new Set(s.toLowerCase().replace(/[^a-z0-9.]+/g, ' ').split(' ').filter(w => w.length > 1))

const wildcard = (p: string) => p.replace(/\/(PAT-[A-Za-z0-9]+|P\d{3,}|[0-9a-f-]{16,}|\d+)(?=\/|$)/g, '/*')

function resolve(method: string, path: string, query: string, reqBody = ''): Recorded | null {
  const exact = `${method} ${path}${query}`
  if (sequences.has(exact)) return pick(sequences.get(exact)!, exact, method, reqBody)
  const pkey = `${method} ${path}`
  if (byPath.has(pkey)) return pick(byPath.get(pkey)!, pkey, method, reqBody)
  const w = wildcard(path)
  for (const [k, list] of byPath) {
    const [m, p] = k.split(' ', 2)
    if (m === method && wildcard(p) === w) return pick(list, k, method, reqBody)
  }
  return null
}

// ─── The demo's clock ───
// Every recorded screen carries the timestamps of the day it was recorded, and
// the app measures ages against the clock: a visitor a week later would find each
// panel amber, stale and asking for a retry. So the demo runs on a clock pinned to
// the end of its recording (recorded_at + the last entry's t), ticking forward at
// real speed from there. Dates built from a value are untouched.
const RealDate = Date
function pinClock(demoNow: number) {
  const offset = demoNow - RealDate.now()
  const now = () => RealDate.now() + offset
  window.Date = new Proxy(RealDate, {
    construct: (target, args, newTarget) => Reflect.construct(target, args.length ? args : [now()], newTarget),
    apply: () => new RealDate(now()).toString(),
    get: (target, prop, receiver) => (prop === 'now' ? now : Reflect.get(target, prop, receiver)),
  }) as DateConstructor
}

// ─── Local handlers: rules the demo can run itself instead of replaying ───

/**
 * The patient's NEWS2 SpO2 scale, read from the recorded nurse board: the card
 * the obs sheet was opened from, which is where the real sheet gets it too.
 * Unknown reads as Scale 1, as an unset patients.on_scale_2 does on the server.
 */
function onScale2(patientId: string): boolean {
  const board = byPath.get('GET /api/nurse/board')
  const last = board?.[board.length - 1]
  if (!last) return false
  try {
    const cards = (JSON.parse(last.body)?.patients ?? []) as { patient_id?: string; on_scale_2?: boolean }[]
    return !!cards.find(c => c?.patient_id === patientId)?.on_scale_2
  } catch { return false }
}

function local(method: string, path: string, reqBody: string): Response | null {
  const preview = method === 'POST' ? /\/api\/nurse\/patients\/([^/]+)\/news2-preview$/.exec(path) : null
  if (preview) {
    try {
      const body = JSON.parse(reqBody || '{}')
      return new Response(JSON.stringify(news2(body, onScale2(decodeURIComponent(preview[1])))), { status: 200, headers: { 'content-type': 'application/json' } })
    } catch { return null }
  }
  return null
}

/**
 * What the visitor creates during the session stays visible: a message posted
 * to a room is appended to every later read of that room, the way the server
 * would have listed it. Keyed by path; only list endpoints (JSON arrays) get it.
 */
const created = new Map<string, Record<string, unknown>[]>()

function remember(method: string, path: string, body: string) {
  if (method !== 'POST' || !/\/messages$/.test(path)) return
  try {
    const obj = JSON.parse(body)
    if (obj && typeof obj === 'object' && !Array.isArray(obj)) {
      if (!created.has(path)) created.set(path, [])
      created.get(path)!.push({ ...obj, timestamp: new Date().toISOString() })
    }
  } catch { /* not JSON */ }
}

function withCreated(path: string, body: string): string {
  const extra = created.get(path)
  if (!extra?.length) return body
  try {
    const list = JSON.parse(body)
    if (!Array.isArray(list)) return body
    const ids = new Set(list.map((m: { id?: unknown }) => m?.id))
    return JSON.stringify([...list, ...extra.filter(m => !ids.has(m.id))])
  } catch { return body }
}

/** A recorded reply to a message should carry the text that was actually typed. */
let freshId = Date.now()

function overlayRequest(rec: Recorded, reqBody: string, path = ''): string {
  if (!/application\/json/.test(rec.ct)) return rec.body
  try {
    const req = reqBody ? JSON.parse(reqBody) : {}
    const res = JSON.parse(rec.body)
    if (!res || typeof res !== 'object' || Array.isArray(res)) return rec.body
    let changed = false
    for (const k of ['content', 'text', 'query', 'question']) {
      if (typeof req[k] === 'string' && typeof res[k] === 'string') { res[k] = req[k]; changed = true }
    }
    // A message created now must not reuse the id of the recorded one already in
    // the room, or the panel merges the two and the typed text is lost.
    if (/\/messages$/.test(path) && typeof res.id === 'number') {
      res.id = ++freshId
      res.timestamp = new Date().toISOString()
      // The recorded reply may carry the wrong-patient flag of the message it was
      // recorded for. The real server only raises it when the text names another
      // patient, so keep it only when this text does.
      const fit = res.room_fit as { suspected_patient_name?: string } | undefined
      if (fit && typeof fit === 'object') {
        const name = String(fit.suspected_patient_name || '').toLowerCase().split(/\s+/).filter(Boolean)
        const text = String(req.content || res.content || '').toLowerCase()
        if (name.length && name.some(n => text.includes(n))) (res.room_fit as { message_id?: number }).message_id = res.id
        else delete res.room_fit
      }
      changed = true
    }
    return changed ? JSON.stringify(res) : rec.body
  } catch { return rec.body }
}

function sseStream(body: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder()
  const events = body.split('\n\n').filter(e => e.trim())
  let i = 0
  return new ReadableStream({
    pull(controller) {
      return new Promise<void>(res => {
        if (i >= events.length) { controller.close(); res(); return }
        const chunk = events[i++] + '\n\n'
        // Sage streams a few words per event; 45 ms between events reads as typing.
        window.setTimeout(() => { controller.enqueue(enc.encode(chunk)); res() }, i === 1 ? 350 : 45)
      })
    },
  })
}

const realFetch = window.fetch.bind(window)

// A scripted demo carries only its own workflow's recording. The odd request a
// workflow makes outside it (a chart section that loads on scroll) is answered
// from the persona's recording, fetched once on the first miss.
let fallbackState: 'idle' | 'loading' | 'done' | 'none' = 'idle'
let fallbackPromise: Promise<boolean> | null = null
async function loadFallback(): Promise<boolean> {
  if (fallbackState === 'done') return false
  if (fallbackState === 'none') return false
  if (!fallbackPromise) {
    fallbackState = 'loading'
    fallbackPromise = (async () => {
      try {
        const who = (window as unknown as { __orbDemoPersona?: string }).__orbDemoPersona
        if (!who) { fallbackState = 'none'; return false }
        const res = await realFetch(`${BASE}-data/${who}.json`)
        if (!res.ok) { fallbackState = 'none'; return false }
        const text = await res.text()
        if (!text.trim().startsWith('{')) { fallbackState = 'none'; return false }
        index(JSON.parse(text) as Fixture)
        fallbackState = 'done'
        return true
      } catch { fallbackState = 'none'; return false }
    })()
  }
  return fallbackPromise
}
let readyResolve!: () => void
export const ready = new Promise<void>(r => { readyResolve = r })

window.fetch = async function demoFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const { path, query } = norm(url)
  if (!path.startsWith('/api/')) return realFetch(input, init)
  await ready
  const method = ((init?.method) || (typeof input !== 'string' && !(input instanceof URL) ? input.method : 'GET') || 'GET').toUpperCase()
  const rel = path.replace(/^\/api/, '')
  const reqBody = typeof init?.body === 'string' ? init.body : ''
  const handled = local(method, path, reqBody)
  if (handled) return handled
  let rec = resolve(method, path, query, reqBody)
  if (!rec && await loadFallback()) rec = resolve(method, path, query, reqBody)
  if (!rec) {
    const k = `${method} ${rel}${query}`
    if (!unmatched.has(k)) { unmatched.add(k); console.info('[orb demo] not recorded:', k) }
    // Mutations the recording never saw are accepted so the UI does not stall.
    if (method !== 'GET') return new Response(JSON.stringify({ ok: true, demo: true }), { status: 200, headers: { 'content-type': 'application/json' } })
    return new Response(JSON.stringify({ detail: 'Not recorded for the demo' }), { status: 404, headers: { 'content-type': 'application/json' } })
  }
  const headers = { 'content-type': rec.ct || 'application/json' }
  if (/text\/event-stream/.test(rec.ct)) return new Response(sseStream(rec.body), { status: rec.status, headers })
  // A little latency keeps spinners honest without making the demo feel slow.
  await new Promise(r => setTimeout(r, 40 + Math.random() * 90))
  if (method === 'GET') return new Response(withCreated(path, rec.body), { status: rec.status, headers })
  const body = overlayRequest(rec, reqBody, path)
  if (rec.status < 300) remember(method, path, body)
  return new Response(body, { status: rec.status, headers })
}

function decodeFrames(frames: { t: number; payload: string }[]) {
  const out: { t: number; event: string; args: unknown[] }[] = []
  // The recording spans a long session with quiet stretches; cap every gap so
  // the ward keeps moving (a tick at least every 2.5 s, never a 3-minute wait).
  let prev = frames.length ? frames[0].t : 0
  let t = 0
  for (const f of frames) {
    t += Math.min(Math.max(0, f.t - prev), 2500)
    prev = f.t
    // engine.io message (4) + socket.io event (2) + optional namespace, then a JSON array
    const m = /^42(?:\/[^,]*,)?(\[[\s\S]*\])$/.exec(f.payload)
    if (!m) continue
    try {
      const arr = JSON.parse(m[1]) as unknown[]
      if (typeof arr[0] === 'string') out.push({ t, event: arr[0], args: arr.slice(1) })
    } catch { /* not a socket.io event frame */ }
  }
  return out
}

function clickWhenPresent(selector: string, timeout = 15000): Promise<boolean> {
  return new Promise(res => {
    const start = Date.now()
    const tick = () => {
      const el = document.querySelector<HTMLElement>(selector)
      if (el) { el.click(); res(true); return }
      if (Date.now() - start > timeout) { res(false); return }
      window.setTimeout(tick, 120)
    }
    tick()
  })
}


// ─── Scripted workflow demos ───
// ?script=<id> locks the app to one workflow and lets the website step through it.
const scriptId = params.get('script') || ''
const script: DemoScript | undefined = scriptId ? scriptById(scriptId) : undefined
const startStep = Math.max(0, parseInt(params.get('step') || '0', 10) || 0)

const $$ = (sel: string) => Array.from(document.querySelectorAll<HTMLElement>(sel))
const visible = (el: HTMLElement) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) || el.closest('.shell__nav') !== null

function find(sel: string, text?: RegExp, nth?: number, mustBeVisible = false): HTMLElement | null {
  let els = $$(sel)
  if (text) els = els.filter(e => text.test((e.textContent || '').trim()))
  // Tab panels use display: contents and have no box of their own, so visibility
  // is only demanded for things the script clicks.
  if (mustBeVisible) { const vis = els.filter(visible); if (vis.length) els = vis }
  return els[nth ?? 0] ?? null
}

function until(check: () => boolean, timeout: number): Promise<boolean> {
  return new Promise(res => {
    const t0 = Date.now()
    const tick = () => { if (check()) return res(true); if (Date.now() - t0 > timeout) return res(false); window.setTimeout(tick, 120) }
    tick()
  })
}

function setNative(el: HTMLElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set
  if (setter) setter.call(el, value); else (el as HTMLInputElement).value = value
}

async function perform(a: Action): Promise<void> {
  switch (a.do) {
    case 'wait': {
      const ok = await until(() => !!find(a.sel), a.timeout ?? 20_000)
      if (!ok && !a.optional) throw new Error(`not found: ${a.sel}`)
      return
    }
    case 'waitText': {
      const ok = await until(() => a.re.test(document.body.innerText || ''), a.timeout ?? 20_000)
      if (!ok && !a.optional) throw new Error(`text not found: ${a.re}`)
      return
    }
    case 'waitEnabled': {
      await until(() => { const b = find(a.sel) as HTMLButtonElement | null; return !!b && !b.disabled }, a.timeout ?? 20_000)
      return
    }
    case 'click': {
      const ok = await until(() => !!find(a.sel, a.text, a.nth, true), a.optional ? 3000 : 15_000)
      const el = ok ? find(a.sel, a.text, a.nth, true) : null
      if (!el) { if (a.optional) return; throw new Error(`nothing to click: ${a.sel}`) }
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      el.click()
      await new Promise(r => setTimeout(r, 250))
      return
    }
    case 'fill': {
      const ok = await until(() => !!find(a.sel), 15_000)
      const el = ok ? find(a.sel) : null
      if (!el) throw new Error(`nothing to fill: ${a.sel}`)
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      el.focus()
      setNative(el, a.value)
      el.dispatchEvent(new Event('input', { bubbles: true }))
      el.dispatchEvent(new Event('change', { bubbles: true }))
      await new Promise(r => setTimeout(r, 200))
      return
    }
    case 'select': {
      const ok = await until(() => !!find(a.sel), 15_000)
      const el = ok ? (find(a.sel) as HTMLSelectElement | null) : null
      if (!el) throw new Error(`no select: ${a.sel}`)
      await until(() => Array.from(el.options).some(o => a.text.test(o.textContent || '')), 10_000)
      const opt = Array.from(el.options).find(o => a.text.test(o.textContent || ''))
      if (opt) { setNative(el, opt.value); el.dispatchEvent(new Event('change', { bubbles: true })) }
      return
    }
    case 'press': {
      const el = (a.sel ? find(a.sel) : null) ?? (document.activeElement as HTMLElement | null)
      if (!el) return
      el.focus()
      const init = { key: a.key, code: a.key, bubbles: true, cancelable: true }
      el.dispatchEvent(new KeyboardEvent('keydown', init))
      el.dispatchEvent(new KeyboardEvent('keypress', init))
      el.dispatchEvent(new KeyboardEvent('keyup', init))
      await new Promise(r => setTimeout(r, 300))
      return
    }
    case 'scroll': {
      const el = find(a.sel)
      if (!el) { if (a.optional) return; throw new Error(`nothing to scroll to: ${a.sel}`) }
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      await new Promise(r => setTimeout(r, 400))
      return
    }
    case 'sleep':
      await new Promise(r => setTimeout(r, a.ms))
      return
  }
}

let stepDone = -1
let running = false

async function runStep(n: number) {
  if (!script) return
  if (running) return
  running = true
  try {
    for (let i = stepDone + 1; i <= n && i < script.steps.length; i++) {
      let ok = true
      for (const action of script.steps[i].actions) {
        try { await perform(action) } catch (e) { ok = false; console.warn('[orb demo] step', i + 1, String(e)) }
      }
      stepDone = i
      window.parent?.postMessage({ type: 'orb-demo-stepped', n: i, ok, caption: script.steps[i].caption, total: script.steps.length }, '*')
    }
  } finally {
    running = false
  }
}

function lockToWorkflow() {
  const style = document.createElement('style')
  style.textContent = `
    /* One workflow at a time: no sidebar, no way to wander into other modules. */
    .shell__sidebar { display: none !important; }
    .shell__main { margin-left: 0 !important; }
    #btn-admit-patient { display: none !important; }
  `
  document.head.appendChild(style)
  // Shortcuts open the palette and the help sheet; neither belongs in a scripted demo.
  window.addEventListener('keydown', e => {
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName || '') || (e.target as HTMLElement)?.isContentEditable
    if (((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') || (!typing && e.key === '?')) { e.stopImmediatePropagation(); e.preventDefault() }
  }, true)
  window.addEventListener('message', e => {
    const d = e.data
    if (!d || typeof d !== 'object') return
    if (d.type === 'orb-demo-run' && typeof d.n === 'number') void runStep(d.n)
    // A parent that attached its listener late can ask for the announcement again.
    if (d.type === 'orb-demo-hello' && script) {
      window.parent?.postMessage({ type: 'orb-demo-script', id: script.id, title: script.title, captions: script.steps.map(st => st.caption) }, '*')
      if (stepDone >= 0) window.parent?.postMessage({ type: 'orb-demo-stepped', n: stepDone, ok: true, caption: script.steps[stepDone].caption, total: script.steps.length }, '*')
    }
  })
}

async function boot() {
  // Recordings live beside the build (public/demo-data on the site), so rebuilding the demo cannot wipe them.
  // A scripted demo loads only its own workflow's recording; the whole persona is the fallback.
  const who = script ? script.persona : persona
  // A dev server answers a missing file with the app's HTML and a 200, so a
  // fixture only counts when the body is JSON.
  const fetchJson = async (url: string): Promise<Fixture | null> => {
    try {
      const r = await realFetch(url)
      if (!r.ok) return null
      const text = await r.text()
      if (!text.trim().startsWith('{')) return null
      return JSON.parse(text) as Fixture
    } catch { return null }
  }
  let fx = script ? await fetchJson(`${BASE}-data/flow-${script.id}.json`) : null
  if (fx) (window as unknown as { __orbDemoPersona?: string }).__orbDemoPersona = who
  else { fx = await fetchJson(`${BASE}-data/${who}.json`); fallbackState = 'done' }
  if (!fx) throw new Error(`demo fixture for ${who} missing`)
  if (fx.recorded_at) {
    const end = [...fx.responses.map(r => r.t), ...(fx.frames || []).map(f => f.t)].reduce((a, b) => Math.max(a, b), 0)
    pinClock(fx.recorded_at + end)
  }
  if (script) lockToWorkflow()
  index(fx)
  loaded = true
  if (fx.user) localStorage.setItem('orb_user', JSON.stringify(fx.user))
  localStorage.setItem('orb_theme', params.get('theme') === 'light' ? 'light' : 'dark')
  document.cookie = 'orb_csrf=demo; path=/'
  window.__orbDemoFrames = decodeFrames(fx.frames || [])
  readyResolve()
  const s = io()
  if (!s.connected) s.connect()

  // Land on the requested tab once the shell has rendered.
  if (tab) {
    await clickWhenPresent(`#nav-${tab}`)
    if (subtab) await clickWhenPresent(`#admin-tab-${subtab}, #nurse-tab-${subtab}, [data-subtab="${subtab}"]`)
    if (open) await clickWhenPresent(`#${open}`)
  }
  if (script) {
    await until(() => !!document.querySelector('#nav-today, #nav-shift, #nav-helix, #nav-admin, #panel-surgical-suite, #bridge-logout, .shell__header, .bridge__header-right'), 15_000)
    window.parent?.postMessage({ type: 'orb-demo-script', id: script.id, title: script.title, captions: script.steps.map(st => st.caption) }, '*')
    await runStep(startStep)
  }
  window.parent?.postMessage({ type: 'orb-demo-ready', persona: who, tab, script: script?.id }, '*')
}

boot().catch(e => {
  console.error('[orb demo]', e)
  readyResolve()
  window.parent?.postMessage({ type: 'orb-demo-error', message: String(e) }, '*')
})

export const isLoaded = () => loaded
