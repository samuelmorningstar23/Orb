/**
 * The refusal wording is shared BECAUSE it is easy to get wrong in one place.
 *
 * Two properties, both clinical-facing:
 *   1. it never asserts the patient does not exist — the server's 404 is
 *      deliberately ambiguous between "no such id" and "not your department",
 *      and the UI must not resolve that ambiguity it was given on purpose;
 *   2. it rewords ONLY that refusal, so a network failure or a missing snapshot
 *      still reaches the clinician as itself.
 */
import { describe, expect, it } from 'vitest'
import { refusalMessage } from './scopeRefusal'

describe('refusalMessage', () => {
  it('rewords only the ambiguous server refusal, leaving other errors alone', () => {
    expect(refusalMessage('Patient not found')).toMatch(/not available to you/i)
    expect(refusalMessage('patient not found')).toMatch(/outside your department/i)
    expect(refusalMessage('  Patient not found  ')).toMatch(/not available to you/i)
    expect(refusalMessage('No snapshot captured for this patient')).toBeNull()
    expect(refusalMessage('Network error')).toBeNull()
    expect(refusalMessage('Patient not found in ward B')).toBeNull()
    expect(refusalMessage('')).toBeNull()
    expect(refusalMessage(undefined)).toBeNull()
    expect(refusalMessage(null)).toBeNull()
  })

  it('never asserts non-existence — the server refusal is deliberately ambiguous', () => {
    const msg = refusalMessage('Patient not found')!
    expect(msg).toMatch(/may not exist/i)
    expect(msg).toMatch(/or it may be/i)
    expect(msg).not.toMatch(/does not exist/i)
  })
})
