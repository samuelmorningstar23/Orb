import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiIdentityChangedError, ApiStreamError, analyzeLensImage, askBridge,
  assignLensToPatient, getDischargeQrObjectUrl, getSurgeryRiskAssessment,
  handleResponseError, querySage, querySageVoice, setUser, structureNote,
  transcribeAudio, transcribeChunk,
} from './client'

const firstUser = { id: 'SYNTHETIC-A', name: 'Demo A', role: 'doctor' }
const nextUser = { id: 'SYNTHETIC-B', name: 'Demo B', role: 'doctor' }
const expired = vi.fn()
const image = () => new File(['synthetic image'], 'demo.png', { type: 'image/png' })
const audio = () => new Blob(['synthetic audio'], { type: 'audio/webm' })
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status })
const frame = (data: unknown) => `data: ${JSON.stringify(data)}\n\n`
const stream = (events: unknown[]) => new Response(events.map(frame).join(''))
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  localStorage.clear()
  setUser(firstUser)
  expired.mockClear()
  window.addEventListener('orb:session-expired', expired)
})
afterEach(() => {
  window.removeEventListener('orb:session-expired', expired)
  localStorage.clear()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const sageLanes = [
  {
    name: 'text', field: 'content', code: 'sage_stream',
    run: (onChunk = vi.fn()) => querySage('Synthetic question', undefined, onChunk),
  },
  {
    name: 'voice', field: 'token', code: 'sage_voice_stream',
    run: (onChunk = vi.fn()) => querySageVoice('Synthetic question', undefined, onChunk),
  },
]

describe('Sage stream completion and failure', () => {
  it('preserves the existing text event protocol and request thread', async () => {
    const metadata = { patient_id: 'DEMO', timing: { total_ms: 10 } }
    const structured = { concise_answer: 'Synthetic answer' }
    const fetcher = vi.fn(async (_url: RequestInfo | URL, _options?: RequestInit) => new Response([
      'data: malformed compatibility artifact\n\n', frame(null), frame(7),
      frame({ stage: 'retrieving', retrieval_ms: 3 }),
      frame({ content: 'Synthetic ' }), frame({ content: 'answer' }),
      frame({ metadata }), frame({ structured_response: structured }), frame({ done: true }),
    ].join('')))
    vi.stubGlobal('fetch', fetcher)
    const chunks = vi.fn(), meta = vi.fn(), stages = vi.fn()
    expect(await querySage('Demo', 'DEMO', chunks, undefined, meta, { tab: 'sage' }, stages, false, 'thread-demo'))
      .toBe('Synthetic answer')
    expect(chunks.mock.calls).toEqual([['Synthetic '], ['Synthetic answer']])
    expect(meta.mock.calls).toEqual([[metadata], [{ _structured_response: structured }]])
    expect(stages).toHaveBeenCalledWith('retrieving', 3)
    expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toMatchObject({
      query: 'Demo', patient_id: 'DEMO', thinking: false, conversation_id: 'thread-demo', view_context: { tab: 'sage' },
    })
  })

  it.each([undefined, { total_ms: 12, ttft_ms: 2 }])('accepts voice completion with optional legacy timing %j', async timing => {
    vi.stubGlobal('fetch', vi.fn(async () => stream([{ token: 'Synthetic ' }, { token: 'answer' }, { done: true, timing }])))
    const chunks = vi.fn()
    expect(await querySageVoice('Demo', undefined, chunks)).toEqual({ fullResponse: 'Synthetic answer', timing })
    expect(chunks.mock.calls).toEqual([['Synthetic '], ['answer']])
  })

  for (const lane of sageLanes) {
    it.each(['start', 'partial', 'after-done'])(`${lane.name} propagates a terminal error at %s without private details`, async position => {
      const events: unknown[] = position === 'start' ? [] : [{ [lane.field]: 'Partial answer' }]
      if (position === 'after-done') events.push({ done: true })
      events.push({ error: '/private/patient-data: model failed', code: 'synthetic_failure' })
      vi.stubGlobal('fetch', vi.fn(async () => stream(events)))
      const failure = await lane.run().catch(error => error)
      expect(failure).toBeInstanceOf(ApiStreamError)
      expect(failure.code).toBe('synthetic_failure')
      expect(failure.message).toMatch(/could not complete/)
      expect(failure.message).not.toMatch(/private|patient-data|model failed/)
    })

    it.each(['empty', 'partial', 'invalid-done'])(`${lane.name} refuses %s EOF as incomplete`, async kind => {
      const events = kind === 'empty' ? [] : [{ [lane.field]: 'Partial answer' }]
      if (kind === 'invalid-done') events.push({ done: 'true' })
      vi.stubGlobal('fetch', vi.fn(async () => stream(events)))
      const failure = await lane.run().catch(error => error)
      expect(failure).toBeInstanceOf(ApiStreamError)
      expect(failure.code).toBe(`${lane.code}_incomplete`)
    })

    it(`${lane.name} refuses an unreadable body`, async () => {
      vi.stubGlobal('fetch', vi.fn(async () => new Response(null)))
      await expect(lane.run()).rejects.toMatchObject({ code: `${lane.code}_incomplete` })
    })

    it(`${lane.name} does not swallow a callback failure`, async () => {
      vi.stubGlobal('fetch', vi.fn(async () => stream([{ [lane.field]: 'Answer' }, { done: true }])))
      const failure = new Error('Synthetic consumer failure')
      await expect(lane.run(vi.fn(() => { throw failure }))).rejects.toBe(failure)
    })

    it(`${lane.name} cancels transport on a terminal error`, async () => {
      const cancel = vi.fn()
      const body = new ReadableStream({
        start(controller) { controller.enqueue(new TextEncoder().encode(frame({ error: 'Failure' }))) }, cancel,
      })
      vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
      await expect(lane.run()).rejects.toMatchObject({ code: `${lane.code}_failed` })
      expect(cancel).toHaveBeenCalledOnce()
    })

    it(`${lane.name} blocks later chunks and completion after an account changes`, async () => {
      const firstChunk = deferred<void>()
      const cancel = vi.fn()
      let controller!: ReadableStreamDefaultController<Uint8Array>
      const body = new ReadableStream<Uint8Array>({ start(c) { controller = c }, cancel })
      vi.stubGlobal('fetch', vi.fn(async () => new Response(body)))
      const chunks = vi.fn(() => { firstChunk.resolve() })
      const pending = lane.run(chunks).catch(error => error)
      controller.enqueue(new TextEncoder().encode(frame({ [lane.field]: 'First answer' })))
      await firstChunk.promise
      setUser(nextUser)
      controller.enqueue(new TextEncoder().encode(frame({ [lane.field]: 'Old account private answer' }) + frame({ done: true })))
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(chunks.mock.calls).toEqual([['First answer']])
      expect(cancel).toHaveBeenCalledOnce()
      expect(expired).not.toHaveBeenCalled()
    })
  }

  it('rechecks identity between callbacks carried by the same text event', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => stream([
      { content: 'First answer', metadata: { patient_id: 'private' }, structured_response: { concise_answer: 'private' } }, { done: true },
    ])))
    const meta = vi.fn()
    await expect(querySage('Demo', undefined, () => setUser(nextUser), undefined, meta)).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(meta).not.toHaveBeenCalled()
  })
})

const rawRequests = [
  { name: 'Sage text', run: () => querySage('Synthetic question') },
  { name: 'Sage voice', run: () => querySageVoice('Synthetic question') },
  { name: 'Scribe upload', run: () => transcribeAudio(audio()) },
  { name: 'Scribe chunk', run: () => transcribeChunk(audio(), 2) },
  { name: 'Scribe draft', run: () => structureNote('Synthetic transcript', 'soap') },
  { name: 'Bridge', run: () => askBridge('Synthetic question') },
  { name: 'Lens analysis', run: () => analyzeLensImage(image(), {}) },
  { name: 'Lens assignment', run: () => assignLensToPatient(1, 'DEMO') },
  { name: 'Surgery risk', run: () => getSurgeryRiskAssessment('DEMO') },
  { name: 'Discharge QR', run: () => getDischargeQrObjectUrl('/api/vigil/patients/DEMO/qr') },
]

describe('raw response identity and session ownership', () => {
  it.each([null, [], 'proxy failure', 42, true])('keeps malformed HTTP403 envelope %j as a permission refusal', async envelope => {
    const failure = await handleResponseError(json(envelope, 403)).catch(error => error)
    expect(failure).toBeInstanceOf(Error)
    expect(failure).toMatchObject({ status: 403, permissions: { message: 'The request was refused (error 403).' } })
    expect(failure).not.toBeInstanceOf(TypeError)
    expect(expired).not.toHaveBeenCalled()
  })

  for (const request of rawRequests) {
    it(`${request.name} rejects a response received after an account replacement`, async () => {
      const response = deferred<Response>()
      const fetcher = vi.fn(() => response.promise)
      vi.stubGlobal('fetch', fetcher)
      const pending = request.run().catch(error => error)
      setUser(nextUser)
      response.resolve(json({ detail: 'Expired' }, 401))
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(fetcher).toHaveBeenCalledOnce()
      expect(expired).not.toHaveBeenCalled()
    })

    it.each([200, 401])(`${request.name} does not act on an old refresh HTTP%s verdict`, async status => {
      const refresh = deferred<Response>(), started = deferred<void>()
      const fetcher = vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith('/auth/refresh')) { started.resolve(); return refresh.promise }
        return json({ detail: 'Expired' }, 401)
      })
      vi.stubGlobal('fetch', fetcher)
      const pending = request.run().catch(error => error)
      await started.promise
      setUser(nextUser)
      refresh.resolve(json({}, status))
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(expired).not.toHaveBeenCalled()
    })
  }

  it.each([403, 404])('rejects a previous account’s delayed HTTP%s refusal body', async status => {
    const body = deferred<unknown>(), started = deferred<void>()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status, json: () => { started.resolve(); return body.promise } } as Response)))
    const pending = structureNote('Demo', 'soap').catch(error => error)
    await started.promise
    setUser(nextUser)
    body.resolve({ detail: { message: 'Private refusal context', error: 'permission_denied' } })
    expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
    expect(expired).not.toHaveBeenCalled()
  })

  it('guards handler awaits for callers that do not pass an explicit identity', async () => {
    const body = deferred<unknown>()
    const pending = handleResponseError({ status: 403, json: () => body.promise } as Response).catch(error => error)
    setUser(nextUser)
    body.resolve({ detail: 'Old refusal' })
    expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
  })

  it('keeps the existing retry notice when a same-account raw request refreshes successfully', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({})))
    await expect(handleResponseError(json({}, 401))).rejects.toThrow('Session refreshed: please retry')
    expect(expired).not.toHaveBeenCalled()
  })

  it('expires a genuinely dead same-account session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => json({}, 401)))
    await expect(handleResponseError(json({}, 401))).rejects.toMatchObject({ message: 'Session expired', status: 401 })
    expect(expired).toHaveBeenCalledOnce()
  })

  it.each([
    { name: 'Scribe upload', run: () => transcribeAudio(audio()) },
    { name: 'Scribe chunk', run: () => transcribeChunk(audio(), 2) },
    { name: 'Lens assignment', run: () => assignLensToPatient(1, 'DEMO') },
  ])('guards $name after successful JSON decoding', async request => {
    const body = deferred<unknown>(), started = deferred<void>()
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: () => { started.resolve(); return body.promise } } as Response)))
    const pending = request.run().catch(error => error)
    await started.promise
    setUser(nextUser)
    body.resolve({ transcript: 'Old account private dictation', patient_name: 'Private' })
    expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
  })

  it('does not mint a discharge QR object URL after identity changes during blob decoding', async () => {
    const createObjectURL = vi.fn()
    vi.stubGlobal('URL', class extends URL { static createObjectURL = createObjectURL })
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, blob: async () => { setUser(nextUser); return new Blob(['private QR']) } } as Response)))
    await expect(getDischargeQrObjectUrl('/api/vigil/patients/DEMO/qr')).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(createObjectURL).not.toHaveBeenCalled()
  })

  it('accepts genuinely silent ASR chunks and refuses worker failures', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(json({ text: '', chunk_index: 2 }))
      .mockResolvedValueOnce(json({ detail: '/private/model-location: subprocess failed' }, 500))
    vi.stubGlobal('fetch', fetcher)
    expect(await transcribeChunk(audio(), 2)).toEqual({ text: '', chunk_index: 2 })
    const failure = await transcribeChunk(audio(), 3).catch(error => error)
    expect(failure).toBeInstanceOf(ApiStreamError)
    expect(failure).toMatchObject({ message: 'Speech transcription failed. Please retry.', code: 'scribe_transcription_failed' })
    expect(failure.message).not.toMatch(/private|model-location|subprocess/)
  })
})
