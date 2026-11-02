import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiIdentityChangedError, apiFetchText, setUser } from './client'
import * as procurement from './procurement'
import * as dietary from './dietary'
import * as workforce from './workforce'
import * as biomedical from './biomedical'
import * as housekeeping from './housekeeping'
import * as nabh from './nabh'

beforeEach(() => {
  localStorage.clear()
  setUser({ id: 'demo-A', name: 'Demo administrator', role: 'admin' })
  document.cookie = 'orb_csrf=csrf-before; path=/'
})
afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  document.cookie = 'orb_csrf=; Max-Age=0; path=/'
})

describe('domain session transport', () => {
  const domains = [procurement, dietary, workforce, biomedical, housekeeping, nabh]
  it.each(domains)('keeps module-disabled errors typed across all six clients (%#)', async domain => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: {
      error: 'module_disabled', message: 'This module is switched off.',
    } }), { status: 404 })))
    const failure = await domain.getMeta().catch(error => error)
    expect(failure.name).toMatch(/ApiError$/)
    expect(failure.isModuleDisabled).toBe(true)
    expect(failure.status).toBe(404)
    expect(failure.code).toBe('module_disabled')
  })

  it('preserves roster conflicts and re-reads rotated CSRF on the one permitted retry', async () => {
    const conflicts = [{ employee_code: 'DEMO', roster_date: '2026-10-11', reason: 'overlap' }]
    const fetcher = vi.fn().mockResolvedValueOnce(new Response('{}', { status: 401 }))
      .mockImplementationOnce(async () => {
        document.cookie = 'orb_csrf=csrf-after; path=/'
        return new Response('{}')
      }).mockResolvedValueOnce(new Response(JSON.stringify({ detail: {
        error: 'roster_conflict', message: 'Conflicting assignments.', conflicts,
      } }), { status: 409 }))
    vi.stubGlobal('fetch', fetcher)
    const failure = await workforce.assignRoster({ employee_code: 'DEMO', roster_date: '2026-10-11', shift_code: 'D' }).catch(error => error)
    expect(fetcher).toHaveBeenCalledTimes(3)
    expect(fetcher.mock.calls[0][1].headers['X-CSRF-Token']).toBe('csrf-before')
    expect(fetcher.mock.calls[2][1].headers['X-CSRF-Token']).toBe('csrf-after')
    expect(failure).toBeInstanceOf(workforce.WorkforceApiError)
    expect(failure.conflicts).toEqual(conflicts)
    expect(failure.detail.conflicts).toEqual(conflicts)
  })

  it('does not retry a previous account’s mutation after account replacement', async () => {
    const fetcher = vi.fn(async () => {
      setUser({ id: 'demo-B', name: 'Another administrator', role: 'admin' })
      return new Response('{}', { status: 401 })
    })
    vi.stubGlobal('fetch', fetcher)
    await expect(dietary.lockMealPlan(1)).rejects.toBeInstanceOf(ApiIdentityChangedError)
    expect(fetcher).toHaveBeenCalledTimes(1)
  })

  it('retains permission payload and enrolment ticket on domain errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: {
      error: 'mfa_enrollment_required', message: 'Enroll this account.', required: ['nabh.read'],
    } }), { status: 403, headers: { 'X-MFA-Enrollment-Ticket': 'demo-enrolment' } })))
    const failure = await nabh.getMeta().catch(error => error)
    expect(failure).toBeInstanceOf(nabh.NabhApiError)
    expect(failure.code).toBe('mfa_enrollment_required')
    expect(failure.permissions.required).toEqual(['nabh.read'])
    expect(failure.mfaEnrollTicket).toBe('demo-enrolment')
  })

  it('guards identity while a text export body is being decoded', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, status: 200, text: async () => {
      setUser({ id: 'demo-B', name: 'Another administrator', role: 'admin' })
      return 'private export'
    } })))
    await expect(apiFetchText('/nabh/export.md')).rejects.toBeInstanceOf(ApiIdentityChangedError)
  })

  it('uses the shared session transport for Markdown export', async () => {
    const fetcher = vi.fn(async (_input: RequestInfo | URL, _options?: RequestInit) => new Response('# Demo evidence\n'))
    vi.stubGlobal('fetch', fetcher)
    expect(await nabh.generatePackMarkdown({})).toBe('# Demo evidence\n')
    expect(fetcher.mock.calls[0][1]).toMatchObject({ credentials: 'include', method: 'POST', headers: { 'X-CSRF-Token': 'csrf-before' } })
  })
})
