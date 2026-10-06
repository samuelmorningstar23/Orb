/**
 * Where a Verify Queue medicine came from: "From the note signed by Dr X at
 * 04:49 PM on 6 Oct 2026", the sentence that names the drug in a quiet line
 * beneath, and a link that opens the signed note in place, so the pharmacist
 * can check the read against what the doctor signed.
 */
import { useState } from 'react';
import { getSourceNote, type SourceNote } from '../../api/pharmacy';
import { formatClinicalDate, formatClinicalTime } from '../../lib/datetime';

export interface SourceNoteFields {
  id: number;
  drug_name: string;
  source_note_id?: number | null;
  source_sentence?: string | null;
  source_note_signed_by?: string | null;
  source_note_signed_at?: string | null;
}

const QUIET = { fontSize: 12, fontWeight: 400, lineHeight: 1.35, color: 'var(--text-secondary)', marginTop: 2 };

/** "From the note signed by Dr X at 04:49 PM on 6 Oct 2026", or the halves known. */
function sourceNoteLine(row: SourceNoteFields): string {
  const who = (row.source_note_signed_by ?? '').trim();
  const time = formatClinicalTime(row.source_note_signed_at ?? null);
  const day = formatClinicalDate(row.source_note_signed_at ?? null);
  const when = time && day ? ` at ${time} on ${day}` : '';
  return who ? `From the note signed by ${who}${when}` : `From a signed note${when}`;
}

export default function SourceNoteLine({ row }: { row: SourceNoteFields }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState<SourceNote | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  if (row.source_note_id == null) return null;

  async function toggle() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (note || loading) return;
    setLoading(true);
    setError(null);
    try {
      setNote(await getSourceNote(row.id));
    } catch (e) {
      setError((e instanceof Error ? e.message : '') || 'The note could not be opened.');
    } finally {
      setLoading(false);
    }
  }

  const panelId = `source-note-${row.id}`;
  return (
    <div style={QUIET}>
      <div>{sourceNoteLine(row)}</div>
      {row.source_sentence && (
        <div style={{ fontStyle: 'italic', marginTop: 2 }}>&ldquo;{row.source_sentence}&rdquo;</div>
      )}
      <button
        type="button"
        style={{ background: 'none', border: 'none', padding: 0, marginTop: 2, color: 'var(--accent-text)', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
        aria-expanded={open}
        aria-controls={open ? panelId : undefined}
        onClick={toggle}
      >
        {open ? 'Close the note' : 'Open the note'}
      </button>
      {open && (
        <div
          id={panelId}
          role="region"
          aria-label={`Signed note for ${row.drug_name}`}
          style={{ marginTop: 6, padding: '8px 10px', border: '1px solid var(--border-subtle)', borderRadius: 6, background: 'var(--bg-surface)', color: 'var(--text-primary)', whiteSpace: 'pre-wrap' }}
        >
          {loading ? 'Opening the note…' : error ? error : note?.text || 'The note is empty.'}
        </div>
      )}
    </div>
  );
}
