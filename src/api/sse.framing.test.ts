import { describe, expect, it } from 'vitest';
import { MAX_SSE_EVENT_CHARS, parseSSEData, readSSEEvents } from './sse';

function response(text: string, chunk = 1) {
  const bytes = new TextEncoder().encode(text);
  return new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      for (let i = 0; i < bytes.length; i += chunk) controller.enqueue(bytes.slice(i, i + chunk));
      controller.close();
    },
  }));
}
async function all(reply: Response) {
  const result: string[] = [];
  for await (const event of readSSEEvents(reply)) result.push(parseSSEData(event));
  return result;
}
describe('bounded transport-independent SSE framing', () => {
  it.each(['\n', '\r\n', '\r'])('handles %j endings and split Unicode', async (newline) => {
    expect(await all(response(`data: नमस्ते 😀${newline}${newline}data: [DONE]${newline}${newline}`))).toEqual(['नमस्ते 😀', '[DONE]']);
  });
  it('handles mixed endings, multiple events in a chunk and blank padding', async () => {
    expect(await all(response('\r\ndata: a\r\ndata: b\n\ndata: c\r\r', 100))).toEqual(['a\nb', 'c']);
  });
  it('ignores protocol metadata and accepts the optional data-field space', () => {
    expect(parseSSEData('event: token\nid: 1\n: heartbeat\nretry: 1000\ndata:foo\ndata: bar')).toBe('foo\nbar');
  });
  it('rejects a truncated event instead of silently completing', async () => {
    await expect(all(response('data: partial'))).rejects.toThrow('before its last event');
    await expect(all(response('data: partial\n'))).rejects.toThrow('before its last event');
  });
  it('bounds an unterminated message, including many nonempty lines', async () => {
    await expect(all(response('data: ' + 'x'.repeat(MAX_SSE_EVENT_CHARS), 65536))).rejects.toThrow('message limit');
    await expect(all(response(('data: x\n').repeat(MAX_SSE_EVENT_CHARS / 6), 65536))).rejects.toThrow('message limit');
  });
  it('rejects malformed UTF-8 and releases the transport', async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new Uint8Array([0xff])); },
      cancel() { cancelled = true; },
    });
    await expect(all(new Response(stream))).rejects.toThrow();
    expect(cancelled).toBe(true);
    expect(stream.locked).toBe(false);
  });
  it('cancels when the consumer stops reading early', async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) { controller.enqueue(new TextEncoder().encode('data: first\n\ndata: second\n\n')); },
      cancel() { cancelled = true; },
    });
    for await (const event of readSSEEvents(new Response(stream))) {
      expect(event).toBe('data: first');
      break;
    }
    expect(cancelled).toBe(true);
    expect(stream.locked).toBe(false);
  });
});
