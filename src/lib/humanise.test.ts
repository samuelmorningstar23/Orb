/**
 * NO MACHINE STRING REACHES A CLINICIAN.
 *
 * `humaniseEnum` already covers snake_case enums. `humaniseSignGateReason`
 * covers the Scribe sign gate, whose reasons are built server-side with an
 * f-string — `f"verified={v.get('verified')}"` — and so arrive as the literal
 * `verified=None`. That string was printed inside the consent dialog that asks
 * a doctor to type SIGN and accept an unverified AI note. `None` is a Python
 * word; it reads as a missing field, not as "this was never checked".
 */
import { describe, expect, it } from 'vitest'
import { humaniseEnum, humaniseSignGateReason } from './humanise'

describe('humaniseEnum', () => {
  it('sentence-cases a snake_case enum', () => {
    expect(humaniseEnum('insufficient_data')).toBe('Insufficient data')
  })

  it('keeps conventional casing for acronyms', () => {
    expect(humaniseEnum('phi_access')).toBe('PHI access')
    expect(humaniseEnum('ai_inference')).toBe('AI inference')
  })

  it('keeps stores and facilities acronyms upper case', () => {
    expect(humaniseEnum('ppe')).toBe('PPE')
    expect(humaniseEnum('grn_1')).toBe('GRN 1')
    expect(humaniseEnum('uom')).toBe('UOM')
    expect(humaniseEnum('icu_bed')).toBe('ICU bed')
    expect(humaniseEnum('surgical_consumable')).toBe('Surgical consumable')
  })

  it('falls back rather than printing an empty label', () => {
    expect(humaniseEnum(null)).toBe('—')
    expect(humaniseEnum('', 'not assessed')).toBe('not assessed')
  })
})

describe('humaniseSignGateReason', () => {
  it('never leaves a Python None in front of the signing clinician', () => {
    const shown = humaniseSignGateReason('verified=None')
    expect(shown).not.toMatch(/None/)
    expect(shown).not.toMatch(/verified=/)
    expect(shown).toBe('No verification verdict was recorded for this note.')
  })

  it('distinguishes "never checked" from "checked and failed"', () => {
    expect(humaniseSignGateReason('verified=False'))
      .toBe('The automated check did not confirm this note against what was dictated or typed.')
    expect(humaniseSignGateReason('verified=None'))
      .not.toBe(humaniseSignGateReason('verified=False'))
  })

  it('reads a warning/omission bucket as words', () => {
    expect(humaniseSignGateReason('warning: unsupported_medication'))
      .toBe('Warning: unsupported medication')
    expect(humaniseSignGateReason('omission: high')).toBe('Omission: high')
  })

  it('leaves a reason that is already a sentence exactly as written', () => {
    expect(humaniseSignGateReason('no verification on record'))
      .toBe('no verification on record')
    expect(humaniseSignGateReason('verification could not complete'))
      .toBe('verification could not complete')
  })

  it('humanises an unrecognised machine-shaped reason rather than printing it raw', () => {
    expect(humaniseSignGateReason('model_unavailable')).toBe('Model unavailable')
  })

  it('drops an empty reason instead of rendering a blank bullet', () => {
    expect(humaniseSignGateReason('')).toBe('')
    expect(humaniseSignGateReason(null)).toBe('')
  })
})
