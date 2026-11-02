import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiIdentityChangedError, apiFetch, setUser } from './client'
import { fetchWardPackHtml } from './downtime'
import { getRosterFields, listRosterRuns, previewRoster } from './rosterImport'
import { downloadCensusCsv } from './adt/census'
import { AdtApiError } from './adt'
import { pharmacyStoreDownload, PharmacyStoreApiError } from './pharmacyStore'
import { fetchRoundsFile } from './wardRecord'
import { previewNews2 } from './nurse'
import { verifyMedication } from './pharmacy'

const owner = { id: 'SYNTHETIC-A', name: 'Demo owner', role: 'admin' }
const replacement = { id: 'SYNTHETIC-B', name: 'Demo replacement', role: 'admin' }
const expired = vi.fn()
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}
const roster = () => new File(['Name,Ward\nSynthetic,Demo'], 'demo.csv', { type: 'text/csv' })
const rounds = () => fetchRoundsFile({ ward: 'DEMO', start: '2026-10-01', end: '2026-10-10', format: 'md' })
const requests = [
  { name: 'downtime HTML', format: 'text', run: () => fetchWardPackHtml('DEMO') },
  { name: 'roster fields', format: 'json', run: () => getRosterFields() },
  { name: 'roster history', format: 'json', run: () => listRosterRuns() },
  { name: 'multipart roster upload', format: 'json', run: () => previewRoster(roster()) },
  { name: 'census CSV', format: 'blob', run: () => downloadCensusCsv() },
  { name: 'pharmacy CSV', format: 'blob', run: () => pharmacyStoreDownload('/stock/export.csv') },
  { name: 'rounds file and provenance', format: 'text', run: rounds },
  { name: 'NEWS2 preview', format: 'json', run: () => previewNews2('DEMO', {}) },
  { name: 'pharmacy verification', format: 'json', run: () => verifyMedication(1) },
]
const downloads = [
  { name: 'census', run: () => downloadCensusCsv(), errorClass: AdtApiError },
  { name: 'pharmacy', run: () => pharmacyStoreDownload('/stock/export.csv'), errorClass: PharmacyStoreApiError },
]

beforeEach(() => {
  localStorage.clear()
  setUser(owner)
  expired.mockClear()
  window.addEventListener('orb:session-expired', expired)
})
afterEach(() => {
  window.removeEventListener('orb:session-expired', expired)
  localStorage.clear()
  document.cookie = 'orb_csrf=; Max-Age=0; path=/'
  vi.unstubAllGlobals()
})

describe('authenticated raw wrapper ownership', () => {
  for (const request of requests) {
    it(`${request.name} refuses a response received after account replacement`, async () => {
      const reply = deferred<Response>()
      const fetcher = vi.fn(() => reply.promise)
      vi.stubGlobal('fetch', fetcher)
      const pending = request.run().catch(error => error)
      setUser(replacement)
      reply.resolve(json({ detail: 'Expired' }, 401))
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(fetcher).toHaveBeenCalledOnce()
      expect(expired).not.toHaveBeenCalled()
    })

    it(`${request.name} checks ownership after decoding its successful body`, async () => {
      const body = deferred<unknown>(), started = deferred<void>()
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, headers: new Headers(),
        [request.format]: () => { started.resolve(); return body.promise },
      } as unknown as Response)))
      const pending = request.run().catch(error => error)
      await started.promise
      setUser(replacement)
      body.resolve(request.format === 'blob' ? new Blob(['private export']) : request.format === 'text' ? 'private document' : { private: 'old account data' })
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(expired).not.toHaveBeenCalled()
    })

    it(`${request.name} checks ownership after decoding a refused response`, async () => {
      const body = deferred<unknown>(), started = deferred<void>()
      vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 403,
        json: () => { started.resolve(); return body.promise },
      } as Response)))
      const pending = request.run().catch(error => error)
      await started.promise
      setUser(replacement)
      body.resolve({ detail: { error: 'permission_denied', message: 'Private refusal' } })
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(expired).not.toHaveBeenCalled()
    })
  }

  for (const download of downloads) {
    it.each([200, 401])(`${download.name} stops an old account's refresh HTTP%s verdict before any retry`, async status => {
      const refresh = deferred<Response>(), started = deferred<void>()
      const fetcher = vi.fn(async (url: RequestInfo | URL) => {
        if (String(url).endsWith('/auth/refresh')) { started.resolve(); return refresh.promise }
        return json({}, 401)
      })
      vi.stubGlobal('fetch', fetcher)
      const pending = download.run().catch(error => error)
      await started.promise
      setUser(replacement)
      refresh.resolve(json({}, status))
      expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
      expect(fetcher).toHaveBeenCalledTimes(2)
      expect(expired).not.toHaveBeenCalled()
    })

    it(`${download.name} keeps same-account export refresh and one retry`, async () => {
      const fetcher = vi.fn().mockResolvedValueOnce(json({}, 401)).mockResolvedValueOnce(json({}))
        .mockResolvedValueOnce(new Response('synthetic,csv\n'))
      vi.stubGlobal('fetch', fetcher)
      const blob = await download.run()
      expect(blob.size).toBe('synthetic,csv\n'.length)
      expect(fetcher).toHaveBeenCalledTimes(3)
      expect(expired).not.toHaveBeenCalled()
    })

    it(`${download.name} preserves its typed module refusal`, async () => {
      vi.stubGlobal('fetch', vi.fn(async () => json({ detail: { error: 'module_disabled', message: 'This module is switched off.' } }, 404)))
      const failure = await download.run().catch(error => error)
      expect(failure).toBeInstanceOf(download.errorClass)
      expect(failure).toMatchObject({ status: 404, code: 'module_disabled', isModuleDisabled: true })
    })
  }

  it('replays multipart bytes only for the same account and rotates CSRF without forcing Content-Type', async () => {
    document.cookie = 'orb_csrf=before; path=/'
    const fetcher = vi.fn().mockResolvedValueOnce(json({}, 401)).mockImplementationOnce(async () => {
      document.cookie = 'orb_csrf=after; path=/'
      return json({})
    }).mockResolvedValueOnce(json({ headers: ['Name', 'Ward'], preview_rows: [] }))
    vi.stubGlobal('fetch', fetcher)
    await previewRoster(roster())
    const initial = fetcher.mock.calls[0][1] as RequestInit
    const retry = fetcher.mock.calls[2][1] as RequestInit
    expect(initial.body).toBeInstanceOf(FormData)
    expect(retry.body).toBe(initial.body)
    expect(initial.headers).toEqual({ 'X-CSRF-Token': 'before' })
    expect(retry.headers).toEqual({ 'X-CSRF-Token': 'after' })
    expect(fetcher.mock.calls[0][0]).toMatch(/\/admin\/roster-import\/preview$/)
  })

  it('keeps same-account HTML bytes and a server-authored scope refusal', async () => {
    const html = '<!doctype html>\n<div>synthetic ward pack</div>\n'
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response(html))
      .mockResolvedValueOnce(json({ detail: 'This patient is outside your ward.' }, 404)))
    expect(await fetchWardPackHtml('DEMO')).toBe(html)
    await expect(fetchWardPackHtml('DEMO')).rejects.toMatchObject({ message: 'This patient is outside your ward.', status: 404 })
  })

  it('preserves the pharmacist acknowledgement payload for the starting account', async () => {
    const safety = { blocking: true, allergy_alerts: [{ message: 'Synthetic alert' }] }
    vi.stubGlobal('fetch', vi.fn(async () => json({ detail: { message: 'Acknowledgement required.', safety } }, 428)))
    expect(await verifyMedication(1)).toEqual({ code: 'severe_ack_required', message: 'Acknowledgement required.', safety })
  })

  it('refuses a pharmacist acknowledgement decoded after account replacement', async () => {
    const body = deferred<unknown>(), started = deferred<void>()
    vi.stubGlobal('fetch', vi.fn(async () => ({ status: 428, json: () => { started.resolve(); return body.promise } } as Response)))
    const pending = verifyMedication(1).catch(error => error)
    await started.promise
    setUser(replacement)
    body.resolve({ detail: { safety: { private: 'old account alert' } } })
    expect(await pending).toBeInstanceOf(ApiIdentityChangedError)
  })

  it.each([null, [], 'proxy', 42, true])('keeps malformed shared HTTP403 envelope %j typed', async envelope => {
    vi.stubGlobal('fetch', vi.fn(async () => json(envelope, 403)))
    const failure = await apiFetch('/synthetic-refusal').catch(error => error)
    expect(failure).toMatchObject({ status: 403, permissions: { message: 'The request was refused (error 403).' } })
    expect(failure).not.toBeInstanceOf(TypeError)
  })
})
