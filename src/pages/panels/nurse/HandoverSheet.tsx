import { useCallback, useEffect, useMemo, useState } from 'react'
import { getUser } from '../../../api/client'
import {
  getIpassDraft,
  getRecentHandoffs,
  saveIpass,
  type IpassDraft,
  type IpassHandoffRecord,
} from '../../../api/ipass'
import { currentUserName, type NursePatientCard } from '../../../api/nurse'
import { isOwnFreshDraft, NURSE_DRAFT_PREFIX, type DraftEnvelope } from '../../../lib/nurseDrafts'
import { assertOnlineForWrite } from '../../../lib/offline/writeGuard'
import { refusalMessage } from '../../../lib/scopeRefusal'
import News2Badge from '../../../components/News2Badge'
import { clockTime } from './time'
import { HANDOVER_NOTE_HEADING, handoverNoteFrom } from './handoverNote'
import { formatClinicalDateTime, parseUTC } from '../../../lib/datetime'

/**
 * End-of-shift handover, over the whole list rather than one patient at a time.
 *
 * The I-PASS assembly itself already exists (server/services/ipass_service.py +
 * api/ipass.ts): it drafts Illness severity / Patient summary / Action list /
 * Situation awareness / Synthesis from the last ~12h. This surface does the
 * thing that was missing — walking a nurse down every patient they are handing
 * over, showing what has been handed over and what has not, and letting them
 * add the part no auto-draft can know: what they are worried about.
 *
 * Nurse commentary is appended to the rendered note under an explicit heading,
 * so a reader can always tell the auto-drafted content from the human's.
 */

interface Props {
  patients: NursePatientCard[]
}

type DraftState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; draft: IpassDraft }
  | { status: 'error'; message: string }

const noteKey = (patientId: string) => `${NURSE_DRAFT_PREFIX}handover-note:${patientId}`

/** How far back a saved handover still counts: the I-PASS window itself
 *  (ipass_service.HANDOFF_WINDOW_HOURS). Long enough to cover a 12-hour shift
 *  and its overlap, short enough that yesterday's note is not read as today's. */
const RECENT_HANDOVER_HOURS = 12

/** A handover saved by someone else in the window: what the incoming nurse
 *  needs at the top of the patient. */
interface ReceivedHandover {
  at: string
  by: string
  note: string | null
}

/** Newest saved handover in the window, split into this nurse's own and
 *  anyone else's. Records arrive newest first. */
function splitRecent(records: IpassHandoffRecord[], ownerId: string | null, now: number) {
  const cutoff = now - RECENT_HANDOVER_HOURS * 3_600_000
  let mine: IpassHandoffRecord | null = null
  let theirs: IpassHandoffRecord | null = null
  for (const r of records) {
    const at = parseUTC(r.created_at).getTime()
    if (isNaN(at) || at < cutoff) continue
    if (ownerId && r.author_id === ownerId) mine ??= r
    else theirs ??= r
  }
  return { mine, theirs }
}

/** A typed-but-unsaved handover comment, stamped so it can be scoped to the
 *  nurse who wrote it — the same envelope the observation drafts carry. */
interface HandoverNoteDraft extends DraftEnvelope {
  text: string
  savedAt: number
}

/**
 * Restore this nurse's own interrupted handover comment — and nobody else's.
 *
 * The logout sweep already clears these keys, but logout is the path that runs
 * when nothing goes wrong. A dead battery, a force-closed browser or a killed
 * tab leaves the comment on a shared tablet, and the next nurse to open the
 * handover sheet would read what the previous one typed about a named patient.
 * So ownership is checked on READ, which no crash can skip.
 *
 * Anything not provably this nurse's — a stranger's draft, a shift-old one, or
 * a bare-string note written before this stamping existed — is not just
 * ignored, it is REMOVED, so it stops sitting on the device for anyone with
 * devtools.
 */
function readNote(patientId: string, ownerId: string | null): string | null {
  const key = noteKey(patientId)
  try {
    const raw = localStorage.getItem(key)
    if (!raw) return null
    let parsed: HandoverNoteDraft | null = null
    try {
      parsed = JSON.parse(raw) as HandoverNoteDraft
    } catch {
      parsed = null
    }
    if (!parsed || typeof parsed !== 'object' || typeof parsed.text !== 'string'
        || !isOwnFreshDraft(parsed, ownerId)) {
      localStorage.removeItem(key)
      return null
    }
    return parsed.text
  } catch {
    return null
  }
}

export default function HandoverSheet({ patients }: Props) {
  // Who a draft comment belongs to. Read from the stored session rather than
  // AuthContext, the same way ObsEntrySheet does, so this leaf stays off App.
  const [ownerId] = useState<string | null>(() => getUser()?.id ?? null)
  const [openId, setOpenId] = useState<string | null>(null)
  const [drafts, setDrafts] = useState<Record<string, DraftState>>({})
  const [notes, setNotes] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<Record<string, string>>({})
  // The last handover someone else saved for each patient, and the patients
  // whose saved handovers could not be read (said, never shown as "none").
  const [received, setReceived] = useState<Record<string, ReceivedHandover>>({})
  const [recentFailed, setRecentFailed] = useState<Record<string, boolean>>({})

  // SAVED HANDOVERS LIVE ON THE SERVER, NOT IN THIS COMPONENT. `saved` used to
  // be state alone, so a handover vanished the moment the nurse left the tab,
  // and the incoming nurse never saw the outgoing one's note at all. Keyed on
  // the patient ids so the board's minute refresh does not refetch the ward.
  const patientIds = useMemo(() => patients.map(p => p.patient_id).join(','), [patients])
  useEffect(() => {
    if (!patientIds) return
    let cancelled = false
    const ids = patientIds.split(',')
    void Promise.all(ids.map(async id => {
      try {
        return { id, records: await getRecentHandoffs(id), failed: false }
      } catch {
        return { id, records: [] as IpassHandoffRecord[], failed: true }
      }
    })).then(results => {
      if (cancelled) return
      const now = Date.now()
      const mineAt: Record<string, string> = {}
      const theirs: Record<string, ReceivedHandover> = {}
      const failed: Record<string, boolean> = {}
      for (const { id, records, failed: f } of results) {
        if (f) { failed[id] = true; continue }
        const split = splitRecent(records, ownerId, now)
        if (split.mine) mineAt[id] = parseUTC(split.mine.created_at).toISOString()
        if (split.theirs) {
          theirs[id] = {
            at: split.theirs.created_at,
            by: split.theirs.author_name || 'another user',
            note: handoverNoteFrom(split.theirs.structured_note),
          }
        }
      }
      // A save made on this screen since the fetch started is newer: keep it.
      setSaved(prev => ({ ...mineAt, ...prev }))
      setReceived(theirs)
      setRecentFailed(failed)
    })
    return () => { cancelled = true }
  }, [patientIds, ownerId])

  // Restore any handover comments typed before an interruption. Same rule as
  // observation entry: nothing a nurse typed disappears on its own — and
  // nothing she did not type appears.
  useEffect(() => {
    const restored: Record<string, string> = {}
    for (const p of patients) {
      const text = readNote(p.patient_id, ownerId)
      if (text) restored[p.patient_id] = text
    }
    if (Object.keys(restored).length) setNotes(prev => ({ ...restored, ...prev }))
  }, [patients, ownerId])

  const loadDraft = useCallback(async (patientId: string) => {
    setDrafts(prev => ({ ...prev, [patientId]: { status: 'loading' } }))
    try {
      const draft = await getIpassDraft(patientId)
      setDrafts(prev => ({ ...prev, [patientId]: { status: 'ready', draft } }))
    } catch (e) {
      const raw = e instanceof Error ? e.message : 'Could not draft handover'
      setDrafts(prev => ({
        ...prev,
        [patientId]: { status: 'error', message: refusalMessage(raw) ?? raw },
      }))
    }
  }, [])

  const toggle = (patientId: string) => {
    const next = openId === patientId ? null : patientId
    setOpenId(next)
    if (next && !drafts[next]) void loadDraft(next)
  }

  const setNote = (patientId: string, value: string) => {
    setNotes(prev => ({ ...prev, [patientId]: value }))
    try {
      if (value.trim()) {
        const draft: HandoverNoteDraft = {
          text: value, savedAt: Date.now(), ownerId: ownerId ?? undefined,
        }
        localStorage.setItem(noteKey(patientId), JSON.stringify(draft))
      } else {
        localStorage.removeItem(noteKey(patientId))
      }
    } catch { /* storage unavailable — in-memory only */ }
  }

  const save = async (patientId: string) => {
    const state = drafts[patientId]
    if (state?.status !== 'ready') return
    setSaving(patientId)
    setSaveError(prev => ({ ...prev, [patientId]: '' }))
    try {
      // A handover saved "offline" that never lands is a shift change with no
      // record of it. The typed note itself is safe — it stays in the local
      // draft under this key and comes back when she retries.
      assertOnlineForWrite('save this handover')
      const note = (notes[patientId] ?? '').trim()
      const payload: IpassDraft = note
        ? {
            ...state.draft,
            plain_text:
              `${state.draft.plain_text}\n\n` +
              `${HANDOVER_NOTE_HEADING}${currentUserName()}, ${formatClinicalDateTime(new Date())}\n${note}\n`,
          }
        : state.draft
      await saveIpass(patientId, payload)
      setSaved(prev => ({ ...prev, [patientId]: new Date().toISOString() }))
      // The comment is now part of a persisted note; the local copy has served
      // its purpose and keeping it would re-appear on the next shift.
      try { localStorage.removeItem(noteKey(patientId)) } catch { /* no-op */ }
    } catch (e) {
      setSaveError(prev => ({
        ...prev,
        [patientId]: e instanceof Error ? e.message : 'Could not save this handover',
      }))
    } finally {
      setSaving(null)
    }
  }

  const handedOver = patients.filter(p => saved[p.patient_id]).length

  return (
    <div className="handover">
      <div className="handover__head">
        <div>
          <h2 className="nurse-section__title">Shift handover</h2>
          <p className="nurse-section__hint">
            I-PASS drafted from each patient's last 12 hours. Review it, add what you are worried
            about, and save. The incoming nurse reads the saved note.
          </p>
        </div>
        <span className="handover__progress" aria-live="polite">
          {handedOver} of {patients.length} handed over
        </span>
      </div>

      {patients.length === 0 && <p className="nurse-empty">No patients to hand over.</p>}

      <ul className="handover__list">
        {patients.map(p => {
          const state = drafts[p.patient_id] ?? { status: 'idle' as const }
          const isOpen = openId === p.patient_id
          const isSaved = !!saved[p.patient_id]
          const incoming = received[p.patient_id]
          return (
            <li className={`handover__item ${isSaved ? 'handover__item--done' : ''}`} key={p.patient_id}>
              <button
                type="button"
                className="handover__toggle"
                onClick={() => toggle(p.patient_id)}
                aria-expanded={isOpen}
                aria-controls={`handover-body-${p.patient_id}`}
              >
                <News2Badge news2={p.news2} labelled={false} />
                <span className="handover__identity">
                  <span className="handover__name">{p.name}</span>
                  <span className="handover__meta">
                    {[p.bed && `Bed ${p.bed}`, p.diagnosis].filter(Boolean).join(' · ') || '—'}
                  </span>
                  {incoming && (
                    <span className="handover__meta">
                      Handed over {formatClinicalDateTime(incoming.at)} by {incoming.by}
                    </span>
                  )}
                </span>
                <span className="handover__state">
                  {isSaved ? `Handed over ${clockTime(saved[p.patient_id])}` : 'Not handed over'}
                </span>
                <span className="handover__chevron" aria-hidden="true">{isOpen ? '−' : '+'}</span>
              </button>

              {isOpen && (
                <div className="handover__body" id={`handover-body-${p.patient_id}`}>
                  {/* What the outgoing nurse said, first: it is the one part of
                      this patient's handover no auto-draft can rebuild. */}
                  {incoming && (
                    <div className="nurse-note nurse-note--info" role="note">
                      <strong>Handed over {formatClinicalDateTime(incoming.at)} by {incoming.by}.</strong>
                      <span>{incoming.note ?? 'No note was added to that handover.'}</span>
                    </div>
                  )}
                  {recentFailed[p.patient_id] && (
                    <div className="nurse-note nurse-note--warn" role="note">
                      Earlier handovers for this patient could not be loaded. Check the chart
                      before relying on this list.
                    </div>
                  )}
                  {state.status === 'loading' && <p className="nurse-empty">Drafting handover…</p>}
                  {state.status === 'error' && (
                    <div className="nurse-note nurse-note--danger" role="alert">
                      {state.message}
                      <button type="button" className="nurse-btn nurse-btn--ghost nurse-btn--compact" onClick={() => void loadDraft(p.patient_id)}>
                        Retry
                      </button>
                    </div>
                  )}
                  {state.status === 'ready' && (
                    <>
                      <dl className="ipass">
                        <div className="ipass__row">
                          <dt>I. Illness severity</dt>
                          <dd>{state.draft.sections.illness_severity.text}</dd>
                        </div>
                        <div className="ipass__row">
                          <dt>P. Patient summary</dt>
                          <dd>{state.draft.sections.patient_summary.text}</dd>
                        </div>
                        <div className="ipass__row">
                          <dt>A. Action list</dt>
                          <dd>
                            {state.draft.sections.action_list.outstanding_items.length > 0 ? (
                              <ul className="ipass__actions">
                                {state.draft.sections.action_list.outstanding_items.map(item => (
                                  <li key={item}>{item}</li>
                                ))}
                              </ul>
                            ) : (
                              'No outstanding actions recorded.'
                            )}
                          </dd>
                        </div>
                        <div className="ipass__row">
                          <dt>S. Situation awareness</dt>
                          <dd>{state.draft.sections.situation_awareness.text}</dd>
                        </div>
                        <div className="ipass__row">
                          <dt>S. Synthesis</dt>
                          <dd>{state.draft.sections.synthesis.text}</dd>
                        </div>
                      </dl>

                      <label className="handover__note-label" htmlFor={`handover-note-${p.patient_id}`}>
                        What are you worried about? (added to the handover under your name)
                      </label>
                      <textarea
                        id={`handover-note-${p.patient_id}`}
                        className="handover__note"
                        rows={3}
                        placeholder="e.g. BP drifting down since 14:00: recheck within the hour."
                        value={notes[p.patient_id] ?? ''}
                        onChange={e => setNote(p.patient_id, e.target.value)}
                      />

                      {saveError[p.patient_id] && (
                        <div className="nurse-note nurse-note--danger" role="alert">{saveError[p.patient_id]}</div>
                      )}

                      <div className="handover__actions">
                        <span className="nurse-footnote">
                          Auto-drafted from recorded data: read it against the patient before handing over.
                        </span>
                        <button
                          type="button"
                          className="nurse-btn nurse-btn--primary"
                          onClick={() => void save(p.patient_id)}
                          disabled={saving === p.patient_id}
                        >
                          {saving === p.patient_id ? 'Saving…' : isSaved ? 'Save again' : 'Save handover'}
                        </button>
                      </div>
                    </>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
