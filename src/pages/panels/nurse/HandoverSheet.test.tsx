/**
 * THE OTHER NURSE DRAFT ON A SHARED WARD TABLET.
 *
 * ObsEntrySheet's in-progress observations are scoped three ways: the logout
 * sweep, an owner stamp checked on read, and an age bound. The handover
 * comments — free clinical text about a named patient, "BP drifting down since
 * 14:00" — had only the first. Logout is the path that runs when nothing goes
 * wrong; a dead battery, a force-closed browser or a killed tab skips it, and
 * the next nurse to open the handover sheet was shown what the previous one had
 * typed. Ownership is now checked on READ, which no crash can skip.
 *
 * What must NOT change is the reason the draft exists: the same nurse, in the
 * same shift, still gets her interrupted comment back.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { NursePatientCard } from '../../../api/nurse'

const getIpassDraft = vi.fn()
const saveIpass = vi.fn()
const getRecentHandoffs = vi.fn()

vi.mock('../../../api/ipass', () => ({
  getIpassDraft: (...a: unknown[]) => getIpassDraft(...a),
  saveIpass: (...a: unknown[]) => saveIpass(...a),
  getRecentHandoffs: (...a: unknown[]) => getRecentHandoffs(...a),
}))

import HandoverSheet from './HandoverSheet'
import { NURSE_DRAFT_MAX_AGE_MS } from '../../../lib/nurseDrafts'

const NOTE_KEY = 'orb:nurse:handover-note:P001'
const NOTE_TEXT = 'BP drifting down since 14:00 — recheck within the hour.'

const PATIENTS = [{
  patient_id: 'P001',
  name: 'Test Patient',
  bed: '4A',
  news2: null,
  diagnosis: 'CAP',
}] as unknown as NursePatientCard[]

/** Sign a nurse in the way api/client stores it — no mock, the real read path. */
function signIn(id: string) {
  localStorage.setItem('orb_user', JSON.stringify({ id, name: 'Nurse', role: 'nurse' }))
}

function writeNote(overrides: Record<string, unknown> = {}) {
  localStorage.setItem(NOTE_KEY, JSON.stringify({
    text: NOTE_TEXT,
    savedAt: Date.now(),
    ownerId: 'NUR001',
    ...overrides,
  }))
}

/** Open P001's row so the note textarea is mounted. */
async function openRow() {
  const toggle = await screen.findByRole('button', { name: /Test Patient/i })
  fireEvent.click(toggle)
  return screen.findByRole('textbox')
}

beforeEach(() => {
  localStorage.clear()
  getIpassDraft.mockReset()
  saveIpass.mockReset()
  getRecentHandoffs.mockReset()
  getRecentHandoffs.mockResolvedValue([])
  getIpassDraft.mockResolvedValue({
    window_hours: 12,
    generated_at: new Date().toISOString(),
    plain_text: 'draft',
    sections: {
      illness_severity: { text: 'Stable' },
      patient_summary: { text: 'summary' },
      action_list: { outstanding_items: [] },
      situation_awareness: { text: 'unchanged' },
      synthesis: { text: 'synthesis' },
    },
  })
  signIn('NUR001')
})

describe('handover comments are scoped to the session, not to the device', () => {
  it("restores this nurse's own interrupted comment (the anti-data-loss feature)", async () => {
    writeNote()
    render(<HandoverSheet patients={PATIENTS} />)
    const textarea = await openRow()
    await waitFor(() => expect((textarea as HTMLTextAreaElement).value).toBe(NOTE_TEXT))
  })

  it("never shows another nurse the previous nurse's comment, and deletes it", async () => {
    writeNote({ ownerId: 'NUR999' })
    render(<HandoverSheet patients={PATIENTS} />)

    // The harm first: nurse NUR001 opening the sheet must not read NUR999's text.
    const textarea = await openRow()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
    expect(screen.queryByText(NOTE_TEXT)).toBeNull()
    // And not merely hidden: gone from the device, so devtools cannot read it.
    await waitFor(() => expect(localStorage.getItem(NOTE_KEY)).toBeNull())
  })

  it('refuses an unstamped comment rather than guessing whose it is', async () => {
    writeNote({ ownerId: undefined })
    render(<HandoverSheet patients={PATIENTS} />)

    await waitFor(() => expect(localStorage.getItem(NOTE_KEY)).toBeNull())
    const textarea = await openRow()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
  })

  it('refuses a bare-string comment left by the build before stamping existed', async () => {
    localStorage.setItem(NOTE_KEY, NOTE_TEXT)
    render(<HandoverSheet patients={PATIENTS} />)

    await waitFor(() => expect(localStorage.getItem(NOTE_KEY)).toBeNull())
    const textarea = await openRow()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
  })

  it('ages a comment out after a shift', async () => {
    writeNote({ savedAt: Date.now() - NURSE_DRAFT_MAX_AGE_MS - 1000 })
    render(<HandoverSheet patients={PATIENTS} />)

    await waitFor(() => expect(localStorage.getItem(NOTE_KEY)).toBeNull())
    const textarea = await openRow()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
  })

  it('restores nothing when nobody is signed in', async () => {
    writeNote()
    localStorage.removeItem('orb_user')
    render(<HandoverSheet patients={PATIENTS} />)

    await waitFor(() => expect(localStorage.getItem(NOTE_KEY)).toBeNull())
    const textarea = await openRow()
    expect((textarea as HTMLTextAreaElement).value).toBe('')
  })
})

describe('scope refusal wording', () => {
  it('shows the careful refusal instead of the raw "Patient not found"', async () => {
    getIpassDraft.mockRejectedValue(new Error('Patient not found'))
    render(<HandoverSheet patients={PATIENTS} />)

    const toggle = await screen.findByRole('button', { name: /Test Patient/i })
    fireEvent.click(toggle)

    expect(await screen.findByText(/not available to you/i)).toBeInTheDocument()
    expect(screen.queryByText(/^Patient not found$/)).toBeNull()
  })
})

/* ══════════════════════════════════════════════════════════════════════════
   A SAVED HANDOVER OUTLIVES THE TAB, AND THE INCOMING NURSE READS IT (F057)
   `saved` was component state only: leave the Handover tab and every "Handed
   over" mark was gone. getRecentHandoffs had no caller, so the incoming nurse
   never saw what the outgoing nurse wrote.
   ══════════════════════════════════════════════════════════════════════════ */
/** "YYYY-MM-DD HH:MM:SS" in UTC, the way scribe_notes.created_at is stored. */
function sqliteUtc(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString().replace('T', ' ').slice(0, 19)
}

function handoff(over: Record<string, unknown>) {
  return {
    id: 1, patient_id: 'P001', author_id: 'NUR001', author_name: 'Nurse',
    raw_transcript: 'I-PASS', note_format: 'handoff', status: 'draft',
    created_at: sqliteUtc(20 * 60_000),
    structured_note: 'I-PASS SHIFT HANDOFF\nP. PATIENT SUMMARY\n   summary',
    ...over,
  }
}

describe('handovers saved earlier this shift', () => {
  it('shows a handover this nurse saved before she left the tab, after a remount', async () => {
    getRecentHandoffs.mockResolvedValue([handoff({})])
    const { unmount } = render(<HandoverSheet patients={PATIENTS} />)
    expect(await screen.findByText('1 of 1 handed over')).toBeInTheDocument()
    unmount()

    render(<HandoverSheet patients={PATIENTS} />)
    expect(await screen.findByText('1 of 1 handed over')).toBeInTheDocument()
    expect(screen.getByText(/^Handed over /)).toBeInTheDocument()
    expect(getRecentHandoffs).toHaveBeenCalledWith('P001')
  })

  it("puts the outgoing nurse's note, her name and the time at the top of the patient", async () => {
    getRecentHandoffs.mockResolvedValue([handoff({
      author_id: 'NUR002', author_name: 'S. Nair',
      structured_note:
        'I-PASS SHIFT HANDOFF\n   summary\n\nHANDOVER NOTE: S. Nair, 3 Oct 2026, 06:40 AM\n' +
        'BP drifting down since 04:00, recheck at 08:00.\n',
    })])
    render(<HandoverSheet patients={PATIENTS} />)
    fireEvent.click(await screen.findByRole('button', { name: /Test Patient/i }))

    const note = await screen.findByRole('note')
    expect(note.textContent).toMatch(/Handed over .* by S\. Nair\./)
    expect(note.textContent).toMatch(/BP drifting down since 04:00, recheck at 08:00\./)
    // Her handover is not the incoming nurse's: nothing is marked done for her.
    expect(screen.getByText('0 of 1 handed over')).toBeInTheDocument()
  })

  it('says when the outgoing handover carried no note of its own', async () => {
    getRecentHandoffs.mockResolvedValue([handoff({ author_id: 'NUR002', author_name: 'S. Nair' })])
    render(<HandoverSheet patients={PATIENTS} />)
    fireEvent.click(await screen.findByRole('button', { name: /Test Patient/i }))
    expect(await screen.findByText('No note was added to that handover.')).toBeInTheDocument()
  })

  it('ignores a handover older than the 12-hour window', async () => {
    getRecentHandoffs.mockResolvedValue([handoff({
      author_id: 'NUR002', author_name: 'S. Nair', created_at: sqliteUtc(13 * 3_600_000),
    })])
    render(<HandoverSheet patients={PATIENTS} />)
    await waitFor(() => expect(getRecentHandoffs).toHaveBeenCalled())
    fireEvent.click(await screen.findByRole('button', { name: /Test Patient/i }))
    await screen.findByRole('textbox')
    expect(screen.queryByText(/by S\. Nair/)).toBeNull()
  })

  it('says the earlier handovers could not be read, rather than showing none', async () => {
    getRecentHandoffs.mockRejectedValue(new Error('Failed to fetch'))
    render(<HandoverSheet patients={PATIENTS} />)
    fireEvent.click(await screen.findByRole('button', { name: /Test Patient/i }))
    expect(await screen.findByText(/Earlier handovers for this patient could not be loaded/)).toBeInTheDocument()
  })
})

describe('handoverNoteFrom', () => {
  it('returns only the nurse\'s words, not the auto-draft above them', async () => {
    const { handoverNoteFrom } = await import('./handoverNote')
    expect(handoverNoteFrom('draft\n\nHANDOVER NOTE: A, 1 Oct 2026, 06:40 PM\nWatch the BP.\n')).toBe('Watch the BP.')
    expect(handoverNoteFrom('draft with no note')).toBeNull()
    expect(handoverNoteFrom(null)).toBeNull()
  })
})
