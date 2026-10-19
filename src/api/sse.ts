/** Bounded SSE framing shared by streaming clients, without session/UI imports. */
export const MAX_SSE_EVENT_CHARS = 1024 * 1024;

export async function* readSSEEvents(response: Response): AsyncGenerator<string> {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder('utf-8', { fatal: true });
  let line = '';
  let lines: string[] = [];
  let size = 0;
  let skipLF = false;
  try {
    while (true) {
      const { done, value } = await reader.read();
      const text = done ? decoder.decode() : decoder.decode(value, { stream: true });
      // CR, LF and CRLF are all legal SSE line endings, even across chunks.
      for (const char of text) {
        if (skipLF) {
          skipLF = false;
          if (char === '\n') continue;
        }
        if (char === '\r' || char === '\n') {
          skipLF = char === '\r';
          if (line === '') {
            const event = lines.join('\n');
            lines = [];
            size = 0;
            if (event.trim()) yield event;
          } else {
            lines.push(line);
            line = '';
          }
        } else {
          size += char.length;
          if (size > MAX_SSE_EVENT_CHARS) throw new Error('The event stream exceeded its message limit.');
          line += char;
        }
      }
      if (done) {
        if (line.trim() || lines.some(value => value.trim())) {
          throw new Error('The event stream ended before its last event was complete.');
        }
        break;
      }
    }
  } finally {
    if (typeof reader.cancel === 'function') await reader.cancel().catch(() => {});
    reader.releaseLock?.();
  }
}

/** Retain the clients' existing permissive reconstruction of non-prefixed lines. */
export function parseSSEData(event: string): string {
  return event.split('\n')
    .filter(line => !line.startsWith(':') && !/^(event|id|retry):/.test(line))
    .map(line => line.startsWith('data:') ? line.slice(5).replace(/^ /, '') : line)
    .join('\n');
}
