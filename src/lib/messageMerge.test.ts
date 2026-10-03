import { describe, it, expect } from 'vitest'
import { mergeById, claimOptimistic, type MergeableMessage } from './messageMerge'

const msg = (over: Partial<MergeableMessage> & { id: number }): MergeableMessage => ({
  sender_id: 'DOC001',
  content: 'hello',
  timestamp: '2026-08-08T10:00:00Z',
  ...over,
})

describe('mergeById', () => {
  it('leaves a clean list untouched', () => {
    const list = [msg({ id: 1 }), msg({ id: 2, content: 'b' })]
    expect(mergeById(list).map(m => m.id)).toEqual([1, 2])
  })

  it('collapses two entries that share an id (the duplicate-key bug)', () => {
    const list = [msg({ id: 79061, client_key: 'opt-1' }), msg({ id: 79061 })]
    const out = mergeById(list)
    expect(out).toHaveLength(1)
    expect(out[0].id).toBe(79061)
  })

  it('lets the later (server) copy win on conflicting fields', () => {
    const list = [
      msg({ id: 7, content: 'draft', timestamp: '2026-08-08T09:00:00Z' }),
      msg({ id: 7, content: 'draft', timestamp: '2026-08-08T10:30:00Z' }),
    ]
    expect(mergeById(list)[0].timestamp).toBe('2026-08-08T10:30:00Z')
  })

  it('keeps the first occurrence position so the thread does not reorder', () => {
    const list = [msg({ id: 5 }), msg({ id: 9, content: 'later' }), msg({ id: 5 })]
    expect(mergeById(list).map(m => m.id)).toEqual([5, 9])
  })
})

describe('claimOptimistic', () => {
  it('replaces my optimistic bubble with the server copy instead of appending', () => {
    const optimistic = msg({ id: 1754640000000, client_key: 'opt-1', content: 'K 6.8 repeat sent' })
    const server = msg({ id: 79061, content: 'K 6.8 repeat sent' })
    const out = claimOptimistic([msg({ id: 12, content: 'earlier' }), optimistic], server)
    expect(out).not.toBeNull()
    expect(out).toHaveLength(2)
    expect(out![1].id).toBe(79061)
    expect(out![1].client_key).toBeUndefined()
  })

  it('does not claim somebody else\'s message with the same text', () => {
    const optimistic = msg({ id: 1754640000000, client_key: 'opt-1', content: 'ok' })
    const other = msg({ id: 400, sender_id: 'NUR001', content: 'ok' })
    expect(claimOptimistic([optimistic], other)).toBeNull()
  })

  it('does not re-claim an entry that already has its real id', () => {
    const settled = msg({ id: 79061, content: 'ok' })
    const another = msg({ id: 79062, content: 'ok' })
    expect(claimOptimistic([settled], another)).toBeNull()
  })

  it('matches two identical sends one-for-one, never both to the same echo', () => {
    const a = msg({ id: 1, client_key: 'opt-a', content: 'same' })
    const b = msg({ id: 2, client_key: 'opt-b', content: 'same' })
    const first = claimOptimistic([a, b], msg({ id: 500, content: 'same' }))!
    expect(first.map(m => m.id)).toEqual([500, 2])
    const second = claimOptimistic(first, msg({ id: 501, content: 'same' }))!
    expect(second.map(m => m.id)).toEqual([500, 501])
    expect(second).toHaveLength(2)
  })

  it('full send race: optimistic -> socket echo -> POST swap yields ONE bubble', () => {
    const optimisticId = 1754640000000
    const optimistic = msg({ id: optimisticId, client_key: 'opt-1', content: 'starting sepsis six' })
    const server = msg({ id: 79061, content: 'starting sepsis six' })

    // (1) composer paints the optimistic bubble
    let thread: MergeableMessage[] = [optimistic]
    // (2) socket message:new arrives with the real id, before the POST resolves
    thread = claimOptimistic(thread, server) ?? [...thread, server]
    // (3) POST response resolves and swaps the (now gone) temp id
    thread = mergeById(
      thread.map(m => (m.id === optimisticId ? { ...server, client_key: undefined } : m)),
    )

    expect(thread).toHaveLength(1)
    expect(new Set(thread.map(m => m.id)).size).toBe(thread.length)
  })

  it('reverse order: POST swap first, then the socket echo, still ONE bubble', () => {
    const optimisticId = 1754640000000
    const optimistic = msg({ id: optimisticId, client_key: 'opt-1', content: 'starting sepsis six' })
    const server = msg({ id: 79061, content: 'starting sepsis six' })

    let thread: MergeableMessage[] = [optimistic]
    thread = mergeById(
      thread.map(m => (m.id === optimisticId ? { ...server, client_key: undefined } : m)),
    )
    // socket echo lands after the swap — the id is already present
    if (!thread.find(m => m.id === server.id)) {
      thread = claimOptimistic(thread, server) ?? [...thread, server]
    }

    expect(thread).toHaveLength(1)
    expect(thread[0].id).toBe(79061)
  })
})
