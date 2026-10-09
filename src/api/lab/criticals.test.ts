/**
 * The words a laboratory step's notice uses for its critical results
 * (lis-15): delivered or not, and what to do either way.
 */
import { describe, expect, it } from 'vitest'

import { criticalsOf, deliveryWords } from './criticals'

describe('criticalsOf', () => {
  it('reads the list a step answers with, and nothing else', () => {
    expect(criticalsOf(null)).toEqual([])
    expect(criticalsOf({ validated: 1 })).toEqual([])
    expect(criticalsOf({ criticals: [{ notification_id: 'LC-1', posted: true, reason: null,
      message_id: 3 }, { bogus: true }] })).toEqual([
      { notification_id: 'LC-1', posted: true, reason: null, message_id: 3 }])
  })
})

describe('deliveryWords', () => {
  it('says nothing when no critical was raised', () => {
    expect(deliveryWords([])).toBeNull()
  })

  it('says a delivered critical was posted and asks for the call to be logged', () => {
    const words = deliveryWords([{ notification_id: 'LC-1', posted: true, reason: null, message_id: 1 }])
    expect(words).toBe("The critical result was posted to the patient's case room. Log the call on Critical results.")
  })

  it('says a change to a critical told earlier was posted and must be phoned', () => {
    const corrected = deliveryWords([
      { notification_id: 'LC-1', posted: true, reason: null, message_id: 4, kind: 'corrected' }])
    expect(corrected).toBe("The change to a critical result reported earlier was posted to the patient's case room. Phone the ward now to tell them the value has changed. Log the call on Critical results.")
    const both = deliveryWords([
      { notification_id: 'LC-1', posted: true, reason: null, message_id: 4, kind: 'changed' },
      { notification_id: 'LC-2', posted: true, reason: null, message_id: 5, kind: 'raised' }])
    expect(both).toContain("The critical result was posted to the patient's case room.")
    expect(both).toContain('The change to a critical result reported earlier was posted')
    expect(both).not.toContain('\u2014')
  })

  it('says an undelivered one must be phoned, and why for a discharged patient', () => {
    const words = deliveryWords([
      { notification_id: 'LC-1', posted: false, reason: 'case_room_closed', message_id: null }])
    expect(words).toContain('could not be posted (the patient has been discharged). Phone the ward now.')
    expect(words).not.toContain('\u2014')
  })
})
