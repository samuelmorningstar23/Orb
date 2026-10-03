/**
 * A refused request must say what was wrong in words. FastAPI's request
 * validation answers 422 with an ARRAY detail, which has no `message`, so every
 * form typo used to surface as "API error: 422" (or "Request failed (422)" on
 * the hospital-module clients).
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, errorDetailMessage } from './client'

const VALIDATION_422 = [
  { type: 'int_parsing', loc: ['body', 'copay_percent_bp'], msg: 'Input should be a valid integer', input: 'ten' },
  { type: 'missing', loc: ['body', 'payer_id'], msg: 'Field required' },
]

describe('errorDetailMessage', () => {
  it('names the first invalid field in words', () => {
    expect(errorDetailMessage(VALIDATION_422, 422)).toBe('Copay percent: input should be a valid integer.')
  })

  it('says a missing field is required', () => {
    expect(errorDetailMessage([{ loc: ['body', 'policy_number'], msg: 'Field required' }], 422))
      .toBe('Policy number is required.')
  })

  it('drops the pydantic "Value error," prefix', () => {
    expect(errorDetailMessage([{ loc: ['body', 'quantity'], msg: 'Value error, Quantity must be a number' }], 422))
      .toBe('Quantity: quantity must be a number.')
  })

  it('passes Orb strings and structured messages through unchanged', () => {
    expect(errorDetailMessage('Co-pay must be between 0 and 100%.', 422)).toBe('Co-pay must be between 0 and 100%.')
    expect(errorDetailMessage({ error: 'x', message: 'The screen is unavailable.' }, 503)).toBe('The screen is unavailable.')
  })

  it('never shows a bare status code', () => {
    expect(errorDetailMessage(undefined, 422)).toBe('The request was refused (error 422).')
    expect(errorDetailMessage([], 400)).toBe('The request was refused (error 400).')
    expect(errorDetailMessage({ error: 'no_message' }, 500)).toMatch(/not answering properly/)
  })
})

describe('apiFetch with a validation array', () => {
  afterEach(() => { vi.unstubAllGlobals() })

  it('throws the readable message and keeps the structured detail', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 422,
      headers: new Headers(),
      json: async () => ({ detail: VALIDATION_422 }),
    })))
    const err = (await apiFetch('/payer/policies', { method: 'POST', body: '{}' }).catch(e => e)) as Error & { status?: number; detail?: unknown }
    expect(err).toBeInstanceOf(Error)
    expect(err.message).toBe('Copay percent: input should be a valid integer.')
    expect(err.message).not.toMatch(/API error/)
    expect(err.status).toBe(422)
    expect(err.detail).toEqual(VALIDATION_422)
  })
})
