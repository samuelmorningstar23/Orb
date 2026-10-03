import { describe, it, expect } from 'vitest'
import { parseSSEData, readSSEEvents } from './client'

describe('parseSSEData', () => {
  it('strips the "data: " prefix from a single-line event', () => {
    expect(parseSSEData('data: {"content":"hi"}')).toBe('{"content":"hi"}')
  })

  it('keeps non-prefixed lines verbatim (permissive reconstruction)', () => {
    expect(parseSSEData('data: a\nb')).toBe('a\nb')
  })

  it('handles a plain [DONE] sentinel', () => {
    expect(parseSSEData('data: [DONE]')).toBe('[DONE]')
  })

  it('returns empty string for an empty event', () => {
    expect(parseSSEData('')).toBe('')
  })
})

function sseResponse(text: string, chunkSize = 7): Response {
  const bytes = new TextEncoder().encode(text)
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      // Emit in small chunks so event boundaries land mid-chunk, exercising the
      // buffer carry-over logic in readSSEEvents.
      for (let i = 0; i < bytes.length; i += chunkSize) {
        controller.enqueue(bytes.slice(i, i + chunkSize))
      }
      controller.close()
    },
  })
  return new Response(stream)
}

describe('readSSEEvents', () => {
  it('yields each event even when boundaries split across read chunks', async () => {
    const body =
      'data: {"content":"one"}\n\n' +
      'data: {"content":"two"}\n\n' +
      'data: [DONE]\n\n'
    const events: string[] = []
    for await (const e of readSSEEvents(sseResponse(body))) {
      events.push(parseSSEData(e))
    }
    expect(events).toEqual(['{"content":"one"}', '{"content":"two"}', '[DONE]'])
  })

  it('reconstructs the streamed content the way the Sage call site does', async () => {
    const body = 'data: {"content":"Hel"}\n\ndata: {"content":"lo"}\n\n'
    let full = ''
    for await (const e of readSSEEvents(sseResponse(body))) {
      const data = JSON.parse(parseSSEData(e))
      if (data.content !== undefined) full += data.content
    }
    expect(full).toBe('Hello')
  })

  it('yields nothing for a body-less response', async () => {
    const events: string[] = []
    for await (const e of readSSEEvents(new Response(null))) {
      events.push(e)
    }
    expect(events).toEqual([])
  })
})
