/** Relay message text to the small HTML subset the thread renders.
 *
 *  Escapes first, then turns the bots' markdown into tags: `- ` bullets,
 *  **bold**, *italic* and _italic_. The caller still runs DOMPurify with a tag
 *  allowlist; this function is not the sanitiser. Line breaks are kept as
 *  newlines and shown by the message style (white-space: pre-line). */
export function formatMessage(text: string): string {
  if (!text) return '';
  const formatted = text.replace(/^(\s*)[-*]\s+/gm, '$1• ');
  return formatted
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/\*\*([^\n*]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^\s\n*](?:[^\n*]*?[^\s\n*])?)\*/g, '<em>$1</em>')
    // _italic_, as the Vigil bot writes its degraded-mode disclaimer. Anchored
    // to a word boundary on both sides (start of line or whitespace before,
    // punctuation, whitespace or end after) so snake_case identifiers and
    // __dunder__ names in a pasted line are left exactly as typed.
    .replace(/(^|[\s(])_([^\s_](?:[^\n_]*?[^\s_])?)_(?=[\s).,;:!?]|$)/gm, '$1<em>$2</em>');
}
