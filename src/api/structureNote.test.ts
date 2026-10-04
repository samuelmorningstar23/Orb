/**
 * structureNote hands the caller everything the stream's done event says.
 *
 * It copied the note id, the verification and the pipeline off the done
 * event but not `status`, so ScribePanel's `if (result.status)
 * setNoteStatus(...)` could never fire. The server sends 'draft' today, which
 * the panel already shows by default, so nothing looked wrong; any other
 * status the server sends would have been dropped on the floor.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { structureNote } from './client'

function sseResponse(events: object[]): Response {
  const body = events.map(e => `data: ${JSON.stringify(e)}\n\n`).join('')
  const bytes = new TextEncoder().encode(body)
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(bytes)
      controller.close()
    },
  }))
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('structureNote', () => {
  it("passes on the done event's status with the rest of the note", async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([
      { content: 'S: chest pain. ' },
      { content: 'A: ACS ruled out.' },
      {
        done: true, id: 42, format: 'soap', status: 'pending_review',
        pipeline: { asr: null }, verification: { status: 'pending' },
      },
    ])))

    const result = await structureNote('transcript', 'soap', 'P1', undefined, true)

    expect(result.id).toBe(42)
    expect(result.structured_note).toBe('S: chest pain. A: ACS ruled out.')
    expect(result.status).toBe('pending_review')
    expect(result.verification).toEqual({ status: 'pending' })
  })

  it('leaves status undefined when the server sends none', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => sseResponse([
      { content: 'Note.' },
      { done: true, id: 7, format: 'soap' },
    ])))

    const result = await structureNote('transcript', 'soap')

    expect(result.id).toBe(7)
    expect(result.status).toBeUndefined()
  })
})
