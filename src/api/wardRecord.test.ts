/**
 * The rounds file download reads the server's refusal in words and keeps the
 * reference and fingerprint the server logged.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { fetchRoundsFile, WardRecordError } from './wardRecord'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('fetchRoundsFile', () => {
  it('turns a 409 for a ward with no interval into the server sentence', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { error: 'no_interval', message: "Set the ward's observation interval first" } }),
      { status: 409, headers: { 'Content-Type': 'application/json' } },
    )))
    const err = await fetchRoundsFile({ ward: 'GM-2', start: '2026-09-01', end: '2026-09-30', format: 'csv' })
      .catch((e: unknown) => e)
    expect(err).toBeInstanceOf(WardRecordError)
    expect((err as WardRecordError).message).toBe("Set the ward's observation interval first")
    expect((err as WardRecordError).status).toBe(409)
    expect((err as WardRecordError).code).toBe('no_interval')
  })

  it('asks for patient numbers only when told to and keeps the file as sent', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('Ward Record rounds file\n', {
      status: 200,
      headers: {
        'Content-Disposition': 'attachment; filename="rounds_GM_1_2026-09-01_2026-09-30.md"',
        'X-Ward-Record-Reference': 'WR-20261006-ABCD1234',
        'X-Content-SHA256': 'a'.repeat(64),
      },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const file = await fetchRoundsFile({ ward: 'GM 1', start: '2026-09-01', end: '2026-09-30', format: 'md' })
    const url = String(fetchMock.mock.calls[0][0])
    expect(url).toContain('/ward-record/rounds.md?')
    expect(url).toContain('ward=GM+1')
    expect(url).not.toContain('include_patient_identifiers')
    expect(file).toEqual({
      fileName: 'rounds_GM_1_2026-09-01_2026-09-30.md',
      text: 'Ward Record rounds file\n',
      reference: 'WR-20261006-ABCD1234',
      sha256: 'a'.repeat(64),
    })

    await fetchRoundsFile({ ward: 'GM 1', start: '2026-09-01', end: '2026-09-30', format: 'csv',
                            includePatientNumbers: true })
    expect(String(fetchMock.mock.calls[1][0])).toContain('include_patient_identifiers=true')
  })
})
