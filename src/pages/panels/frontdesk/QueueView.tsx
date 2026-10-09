/**
 * The front desk's OPD queue while the server's 'opd' feature is on (opd-8).
 *
 * NOT A DISCOVERED DESK VIEW. Its name ends in View.tsx because the plan gives
 * the desk queue this file (opd-9b edits it next), but the OPD queue is one of
 * the desk's two built-in sections (viewRegistry.BUILT_IN_SECTION_IDS), so
 * FrontDeskPanel leaves this file out of its ./frontdesk/*View.tsx glob and
 * renders it inside the built-in 'OPD queue' section instead.
 *
 * What it adds over the flag-off queue:
 *   * each visit's queue priority (with its reason), room and skipped state,
 *     in the server's seeing order (priority, then token);
 *   * a token a doctor's take changed: the slip in the patient's hand shows
 *     the old number, which in that doctor's line is another patient's;
 *   * Priority and room: the desk sets them, a reason required for anything
 *     but normal (ruling OPD-N9);
 *   * Left without being seen: a cancel with the LWBS code, which never raises
 *     a charge;
 *   * live updates (opd-9b): the queue re-reads when the server's content-free
 *     'opd:queue_changed' nudge arrives on the socket (it carries the clinic
 *     and nothing else, so it is never read), and once on reconnecting. The
 *     timed re-read every ORB_OPD_QUEUE_POLL_SECONDS is kept only while the
 *     socket is down, or when the server says this account's socket does not
 *     hear the nudge (`live_updates` false or absent). Because a quiet queue
 *     is not re-read, the waits keep running on screen: every running wait
 *     and the longest wait add the whole minutes since the read
 *     (hooks/useLiveWaits), so a patient waiting 50 minutes never reads 5.
 *
 * Calling a patient in and finishing a consultation belong to the consulting
 * doctor's Outpatients tab in OPD mode (opd.consult; the administrator and
 * the receptionist are refused by the server). A doctor using this screen gets
 * Call in; finishing stays on the doctor's tab, where the signed-note rule
 * (ruling OPD-N8) applies.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { socket } from '../../../api/client'
import { formatWait, waitBand, type OpdQueueResponse } from '../../../api/frontdesk'
import {
  QUEUE_CHANGED_EVENT,
  callOpdVisit,
  cancelOpdVisitWithCode,
  tokenWasReissued,
  updateOpdVisitQueue,
  type CodeLabel,
  type OpdQueueVisit,
} from '../../../api/opd'
import { useFocusTrap } from '../../../hooks/useFocusTrap'
import { liveLongestWait, liveWaitMinutes, useMinutesSinceRead } from '../../../hooks/useLiveWaits'
import { useVisiblePolling } from '../../../hooks/useVisiblePolling'
import './QueueView.css'

/** The desk queue as OPD mode returns it. */
export type OpdModeQueue = Omit<OpdQueueResponse, 'queues'> & {
  opd_mode: true
  poll_seconds: number
  /** opd-9b: this account's socket hears the queue nudge. Absent reads as
   *  false, so a server without it keeps the timed re-read. */
  live_updates?: boolean
  priorities: CodeLabel[]
  cancel_reasons: CodeLabel[]
  queues: (Omit<OpdQueueResponse['queues'][number], 'visits'> & { visits: OpdQueueVisit[] })[]
}

/** Several changes in a burst (a call-in nudges, then the take) re-read once. */
const NUDGE_DEBOUNCE_MS = 400

export interface QueueViewProps {
  queue: OpdModeQueue
  /** Re-read the queue (the panel's loader). */
  onRefresh: () => void
  /** Holds opd.consult: may call a patient in. */
  canCallIn: boolean
}

const STATUS_WORDS: Record<string, string> = {
  waiting: 'Waiting',
  in_consultation: 'In consultation',
  completed: 'Completed',
  cancelled: 'Cancelled',
}

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was saved.'
}

function PriorityRoomDialog({
  visit,
  priorities,
  onClose,
  onSaved,
}: {
  visit: OpdQueueVisit
  priorities: CodeLabel[]
  onClose: () => void
  onSaved: () => void
}) {
  const [priority, setPriority] = useState(visit.priority || 'normal')
  const [reason, setReason] = useState(visit.priority_reason ?? '')
  const [room, setRoom] = useState(visit.room ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const close = useCallback(() => { if (!busy) onClose() }, [busy, onClose])
  const ref = useFocusTrap<HTMLDivElement>(close)
  const needsReason = priority !== 'normal'
  const ready = !busy && (!needsReason || reason.trim().length >= 3)

  const save = async () => {
    if (!ready) return
    setBusy(true)
    setError('')
    try {
      await updateOpdVisitQueue(visit.id, {
        priority,
        priorityReason: needsReason ? reason.trim() : null,
        room: room.trim() || null,
      })
      onSaved()
    } catch (err) {
      setError(errorText(err))
      setBusy(false)
    }
  }

  return createPortal(
    <div className="fd-modal-backdrop" onClick={close}>
      <div className="fd-modal" ref={ref} role="dialog" aria-modal="true"
        aria-labelledby="fdq-priority-title" onClick={e => e.stopPropagation()}>
        <h2 className="fd-modal__title" id="fdq-priority-title">
          Token {visit.token_number}: priority and room
        </h2>
        <div className="fd-field">
          <label htmlFor="fdq-priority">Priority</label>
          <select id="fdq-priority" value={priority} disabled={busy}
            onChange={e => setPriority(e.target.value)}>
            {priorities.map(p => <option key={p.code} value={p.code}>{p.label}</option>)}
          </select>
        </div>
        {needsReason && (
          <div className="fd-field">
            <label htmlFor="fdq-reason">Reason</label>
            <input id="fdq-reason" value={reason} disabled={busy}
              placeholder="For example, aged 82 and unsteady"
              onChange={e => setReason(e.target.value)} />
            <p className="fd-hint">The doctor sees this reason beside the token.</p>
          </div>
        )}
        <div className="fd-field">
          <label htmlFor="fdq-room">Room</label>
          <input id="fdq-room" value={room} disabled={busy} placeholder="For example, 4"
            onChange={e => setRoom(e.target.value)} />
        </div>
        {error && <p className="fd-error" role="alert">{error}</p>}
        <div className="fd-modal__actions">
          <button type="button" className="fd-btn fd-btn--quiet" onClick={close} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="fd-btn fd-btn--primary" onClick={save} disabled={!ready}>
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  )
}

export default function QueueView({ queue, onRefresh, canCallIn }: QueueViewProps) {
  const [busyId, setBusyId] = useState('')
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<OpdQueueVisit | null>(null)

  /* opd-9b: re-read on the server's nudge. The payload is the clinic and
     nothing else, so it is never read. The socket's state decides whether the
     timed re-read runs: only while it is down, or while the server says this
     account does not hear the nudge. */
  const refresh = useRef(onRefresh)
  useEffect(() => { refresh.current = onRefresh }, [onRefresh])
  const [connected, setConnected] = useState<boolean>(() => socket?.connected === true)
  useEffect(() => {
    if (!socket) return
    let timer: ReturnType<typeof setTimeout> | undefined
    const onNudge = () => {
      if (timer) return
      timer = setTimeout(() => { timer = undefined; refresh.current() }, NUDGE_DEBOUNCE_MS)
    }
    // A nudge sent while the socket was down is lost: read once on return.
    const onConnect = () => { setConnected(true); refresh.current() }
    const onDisconnect = () => setConnected(false)
    socket.on(QUEUE_CHANGED_EVENT, onNudge)
    socket.on('connect', onConnect)
    socket.on('disconnect', onDisconnect)
    return () => {
      socket.off(QUEUE_CHANGED_EVENT, onNudge)
      socket.off('connect', onConnect)
      socket.off('disconnect', onDisconnect)
      if (timer) clearTimeout(timer)
    }
  }, [])
  const live = queue.live_updates === true && connected
  useVisiblePolling(onRefresh, Math.max(5, queue.poll_seconds || 30) * 1000, !live)
  /* The waits are the server's as of this read. A quiet queue is not re-read
     while live, so the running ones count on from it here. */
  const sinceRead = useMinutesSinceRead(queue)

  const run = async (visit: OpdQueueVisit, fn: () => Promise<unknown>) => {
    setBusyId(visit.id)
    setError('')
    try {
      await fn()
      onRefresh()
    } catch (err) {
      setError(errorText(err))
    } finally {
      setBusyId('')
    }
  }

  if (!queue.queues.length) {
    return (
      <div className="fd-queues">
        {error && <p className="fd-error" role="alert">{error}</p>}
        <p className="fd-empty">No one is waiting in {queue.department ?? 'any clinic'} today.</p>
      </div>
    )
  }

  return (
    <div className="fd-queues">
      {error && <p className="fd-error" role="alert">{error}</p>}
      {queue.queues.map(q => (
        <section className="fd-card fd-queue" key={`${q.department}:${q.doctor_id ?? 'none'}`}>
          <header className="fd-queue__head">
            <div>
              <h3 className="fd-card__title">{q.doctor_name}</h3>
              <p className="fd-queue__sub">{q.department}</p>
            </div>
            <div className="fd-queue__stats">
              <span className="fd-stat">
                <span className="fd-stat__value">{q.waiting_count}</span>
                <span className="fd-stat__label">waiting</span>
              </span>
              <span className="fd-stat">
                <span className="fd-stat__value">
                  {formatWait(liveLongestWait(q.visits, q.longest_wait_minutes, sinceRead))}
                </span>
                <span className="fd-stat__label">longest wait</span>
              </span>
            </div>
          </header>
          <div className="fd-table-scroll">
            <table className="fd-table">
              <thead>
                <tr>
                  <th scope="col">Token</th>
                  <th scope="col">Patient</th>
                  <th scope="col">Priority</th>
                  <th scope="col">Room</th>
                  <th scope="col">Waiting</th>
                  <th scope="col">Status</th>
                  <th scope="col">Action</th>
                </tr>
              </thead>
              <tbody>
                {q.visits.map(v => {
                  const open = v.status === 'waiting' || v.status === 'in_consultation'
                  const wait = liveWaitMinutes(v, sinceRead)
                  return (
                    <tr key={v.id}>
                      <td className="fd-token">
                        {v.token_number}
                        {tokenWasReissued(v) && (
                          /* A doctor's take gave a new token: the slip still shows the old one. */
                          <span className="fdq-skipped">
                            Slip says token {v.previous_token_number}. Tell the patient their new token.
                          </span>
                        )}
                      </td>
                      <td>
                        <span className="fd-patient-name">{v.patient_name ?? v.patient_id}</span>
                        <span className="fd-patient-mrn">{v.patient_mrn}</span>
                      </td>
                      <td>
                        <span className={`fdq-priority fdq-priority--${v.priority === 'emergency'
                          ? 'emergency' : v.priority === 'normal' ? 'normal' : 'other'}`}>
                          {v.priority_label}
                        </span>
                        {v.priority_reason && <span className="fdq-reason">{v.priority_reason}</span>}
                      </td>
                      <td className="fd-nowrap">{v.room || 'Not set'}</td>
                      <td>
                        <span className={`fd-wait fd-wait--${waitBand(wait)}`}>
                          {formatWait(wait)}
                        </span>
                      </td>
                      <td>
                        <span className={`fd-chip fd-chip--${v.status}`}>{STATUS_WORDS[v.status] ?? v.status}</span>
                        {v.skipped && <span className="fdq-skipped">Skipped, not answered when called</span>}
                        {v.status === 'cancelled' && v.cancel_reason_label && (
                          <span className="fdq-reason">{v.cancel_reason_label}</span>
                        )}
                      </td>
                      <td className="fd-actions-cell">
                        <div className="fd-actions">
                          {canCallIn && v.status === 'waiting' && (
                            <button type="button" className="fd-btn" disabled={busyId === v.id}
                              onClick={() => run(v, () => callOpdVisit(v.id))}>
                              Call in
                            </button>
                          )}
                          {open && (
                            <button type="button" className="fd-btn fd-btn--quiet" disabled={busyId === v.id}
                              onClick={() => setEditing(v)}>
                              Priority and room
                            </button>
                          )}
                          {v.status === 'waiting' && (
                            <button type="button" className="fd-btn fd-btn--quiet" disabled={busyId === v.id}
                              onClick={() => run(v, () => cancelOpdVisitWithCode(
                                v.id, 'left_without_being_seen', 'Left without being seen'))}>
                              Left without being seen
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </section>
      ))}
      {editing && (
        <PriorityRoomDialog
          visit={editing}
          priorities={queue.priorities}
          onClose={() => setEditing(null)}
          onSaved={() => { setEditing(null); onRefresh() }}
        />
      )}
    </div>
  )
}
