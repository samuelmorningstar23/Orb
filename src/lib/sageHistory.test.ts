import { describe, expect, it } from 'vitest'
import { sageThreads } from './sageHistory'
import type { SageHistoryMessage } from '../api/client'

const row = (conversation_id: string | null, role: string, content: string, timestamp = '2026-10-11 10:00:00', patient_name?: string): SageHistoryMessage =>
  ({ conversation_id, role, content, timestamp, ...(patient_name ? { metadata: { patient_name } } : {}) })

describe('Sage history threads', () => {
  it('groups interleaved multi-turn threads under their first question and keeps original metadata', () => {
    const messages = [row('A', 'user', 'First question'), row('B', 'user', 'Other question'),
      row('A', 'assistant', 'First answer', undefined, 'Demo patient'), row('A', 'user', 'Follow-up'), row('A', 'assistant', 'Second answer')]
    const threads = sageThreads(messages)
    expect(threads).toHaveLength(2)
    const a = threads.find(thread => thread.conversationId === 'A')!
    expect(a.content).toBe('First question')
    expect(a.messages.map(message => message.content)).toEqual(['First question', 'First answer', 'Follow-up', 'Second answer'])
    expect(a.messages[1]).toBe(messages[2])
    expect(a.patientName).toBe('Demo patient')
  })
  it('uses local calendar days across month boundaries and keeps unthreaded history separate', () => {
    const now = new Date(2026, 10, 1, 1, 0)
    const stamp = (day: number) => new Date(2026, 9, day, 23, 0).toISOString()
    const result = sageThreads([row('today', 'user', 'Today', now.toISOString()), row('yesterday', 'user', 'Yesterday', stamp(31)),
      row('older', 'user', 'Older', stamp(30)), row(null, 'user', 'Legacy', now.toISOString())], now)
    expect(Object.fromEntries(result.map(thread => [thread.conversationId, thread.bucket])))
      .toEqual({ today: 'Today', yesterday: 'Yesterday', older: 'Earlier', null: 'Earlier' })
  })
  it('does not mislabel a multi-patient thread as belonging to one patient', () => {
    const result = sageThreads([row('A', 'user', 'Compare'), row('A', 'assistant', 'One', undefined, 'Demo One'), row('A', 'assistant', 'Two', undefined, 'Demo Two')])
    expect(result[0].patientName).toBe('Multiple patients')
  })
  it('sorts by latest activity and handles malformed/missing timestamps conservatively', () => {
    const result = sageThreads([row('A', 'user', 'A', ''), row('B', 'user', 'B', 'invalid'), row('C', 'user', 'C')], new Date(2026, 9, 11))
    expect(result.map(thread => thread.conversationId)).toEqual(['C', 'B', 'A'])
    expect(result[1].bucket).toBe('Earlier')
  })
  it('recovers a valid activity date after an invalid timestamp in the same thread', () => {
    const result = sageThreads([row('A', 'user', 'A', 'invalid'), row('A', 'assistant', 'Answer', '2026-10-11T10:00:00Z')])
    expect(result[0].timestamp).toBe('2026-10-11T10:00:00Z')
  })
})
