/**
 * Front Desk, Display boards (opd-9): the waiting-room token boards.
 *
 * The administrator (schedule.manage) makes a board for one clinic and opens
 * its link once on the screen on the wall. The link carries the board's
 * device key in its FRAGMENT (/display/opd#key=...), which a browser never
 * sends to a server; the key is shown here once, when the board is made, and
 * is never stored, so it cannot be shown again: switch the board off and make
 * a new one instead. A board shows token, doctor and room only (ruling
 * OPD-N7), never a patient's name.
 *
 * Shown only while the 'registration' feature is on (the panel discovers this
 * file through ./viewRegistry.ts) and only to schedule.manage holders; the
 * server refuses anyone else, and answers 404 while outpatients (ORB_OPD) is
 * off, which this screen says in words. No patient data is read or shown.
 */
import { useCallback, useEffect, useState } from 'react'

import {
  boardLink,
  createDisplayBoard,
  featureOffOf,
  listDisplayBoards,
  revokeDisplayBoard,
  type DisplayBoard,
  type DisplayBoardList,
} from '../../../api/opdBoards'
import { formatClinicalDateTime } from '../../../lib/datetime'
import type { FrontDeskViewProps } from './viewRegistry'
import './DisplayBoardsView.css'

export const viewId = 'display-boards'
export const viewLabel = 'Display boards'
export const viewOrder = 50
export const viewPermission = 'schedule.manage'

function errorText(err: unknown): string {
  if (err instanceof Error && err.message) return err.message
  return 'Something went wrong. Nothing was changed.'
}

function NewKey({ made, onDone }: { made: { board: DisplayBoard; key: string }; onDone: () => void }) {
  const link = boardLink(window.location.origin, made.key)
  const [copied, setCopied] = useState(false)
  return (
    <section className="fd-card db-key" aria-label="New board link">
      <h3 className="fd-card__title">Open this link on the board&rsquo;s screen now</h3>
      <p className="fd-hint">
        {made.board.label} ({made.board.department}). The link holds the board&rsquo;s key and is
        shown only this once. Anyone with it can see this clinic&rsquo;s tokens, doctors and rooms,
        and nothing else.
      </p>
      <p className="db-key__link" data-testid="board-link">{link}</p>
      <div className="db-key__actions">
        <button
          type="button"
          className="fd-btn"
          onClick={() => {
            void navigator.clipboard?.writeText(link).then(() => setCopied(true)).catch(() => {})
          }}
        >
          {copied ? 'Link copied' : 'Copy link'}
        </button>
        <button type="button" className="fd-btn fd-btn--primary" onClick={onDone}>
          Done, the link is on the screen
        </button>
      </div>
    </section>
  )
}

function BoardRow({ board, onRevoked, onError }: {
  board: DisplayBoard
  onRevoked: () => void
  onError: (message: string) => void
}) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const revoke = async () => {
    setBusy(true)
    try {
      await revokeDisplayBoard(board.id)
      onRevoked()
    } catch (err) {
      onError(errorText(err))
    } finally {
      setBusy(false)
      setConfirming(false)
    }
  }
  return (
    <tr>
      <td>{board.label}</td>
      <td>{board.department}</td>
      <td>
        {formatClinicalDateTime(board.created_at)}
        {board.created_by_name ? `, ${board.created_by_name}` : ''}
      </td>
      <td>
        {board.active
          ? 'Showing tokens'
          : `Switched off ${board.revoked_at ? formatClinicalDateTime(board.revoked_at) : ''}`}
      </td>
      <td>
        {board.active && !confirming && (
          <button type="button" className="fd-btn fd-btn--quiet" onClick={() => setConfirming(true)}>
            Switch off
          </button>
        )}
        {board.active && confirming && (
          <span className="db-confirm">
            <button type="button" className="fd-btn fd-btn--primary" disabled={busy}
              onClick={() => { void revoke() }}>
              {busy ? 'Switching off…' : `Switch off ${board.label}`}
            </button>
            <button type="button" className="fd-btn fd-btn--quiet" disabled={busy}
              onClick={() => setConfirming(false)}>
              Keep it
            </button>
          </span>
        )}
      </td>
    </tr>
  )
}

export default function DisplayBoardsView({ onFeatureOff }: FrontDeskViewProps) {
  const [list, setList] = useState<DisplayBoardList | null>(null)
  const [opdOff, setOpdOff] = useState(false)
  const [error, setError] = useState('')
  const [department, setDepartment] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [made, setMade] = useState<{ board: DisplayBoard; key: string } | null>(null)

  const handle = useCallback((err: unknown): boolean => {
    const feature = featureOffOf(err)
    if (feature === 'registration') { onFeatureOff(); return true }
    if (feature) { setOpdOff(true); return true }
    return false
  }, [onFeatureOff])

  const load = useCallback(async () => {
    try {
      const answer = await listDisplayBoards()
      setList(answer)
      setOpdOff(false)
      setDepartment(prev => (prev && answer.departments.includes(prev) ? prev : answer.departments[0] ?? ''))
    } catch (err) {
      if (!handle(err)) setError(errorText(err))
    }
  }, [handle])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch trigger: the setState is the board list arriving from the server.
  useEffect(() => { void load() }, [load])

  const create = async (event: React.FormEvent) => {
    event.preventDefault()
    if (busy || !department) return
    setBusy(true)
    setError('')
    try {
      const answer = await createDisplayBoard({ department, label })
      setMade(answer)
      setLabel('')
      void load()
    } catch (err) {
      if (!handle(err)) setError(errorText(err))
    } finally {
      setBusy(false)
    }
  }

  if (opdOff) {
    return (
      <div className="fd-card" role="status">
        <p className="fd-empty">
          Token boards show the outpatient queue, so they need Outpatients switched on for this
          site. An administrator can switch it on.
        </p>
      </div>
    )
  }

  return (
    <div className="db-view">
      {made && <NewKey made={made} onDone={() => setMade(null)} />}

      <form className="fd-card db-form" onSubmit={create} aria-labelledby="db-new-title">
        <h3 className="fd-card__title" id="db-new-title">New token board</h3>
        <p className="fd-hint">
          A board shows one clinic&rsquo;s tokens, doctors and rooms in the waiting area, never a
          patient&rsquo;s name. It reads the queue again every
          {` ${list?.poll_seconds ?? 10} `}seconds.
        </p>
        <div className="fd-field">
          <label htmlFor="db-department">Clinic</label>
          <select id="db-department" value={department} disabled={!list || busy}
            onChange={e => setDepartment(e.target.value)}>
            {(list?.departments ?? []).length === 0 && <option value="">No clinic to choose</option>}
            {(list?.departments ?? []).map(d => <option key={d} value={d}>{d}</option>)}
          </select>
        </div>
        <div className="fd-field">
          <label htmlFor="db-label">Where the screen is (optional)</label>
          <input id="db-label" type="text" maxLength={80} value={label} disabled={busy}
            placeholder="For example, Medicine waiting hall" onChange={e => setLabel(e.target.value)} />
        </div>
        {error && <p className="fd-error" role="alert">{error}</p>}
        <div className="db-form__actions">
          <button type="submit" className="fd-btn fd-btn--primary" disabled={busy || !department}>
            {busy ? 'Making the board…' : 'Make board'}
          </button>
        </div>
      </form>

      <section className="fd-card" aria-label="Token boards">
        <h3 className="fd-card__title">Token boards</h3>
        {!list
          ? <p className="fd-empty">Loading the boards…</p>
          : list.boards.length === 0
            ? <p className="fd-empty">No token board has been made yet.</p>
            : (
              <div className="fd-table-scroll">
                <table className="fd-table">
                  <thead>
                    <tr><th>Board</th><th>Clinic</th><th>Made</th><th>State</th><th><span className="db-visually-hidden">Action</span></th></tr>
                  </thead>
                  <tbody>
                    {list.boards.map(b => (
                      <BoardRow key={b.id} board={b} onRevoked={() => { void load() }}
                        onError={setError} />
                    ))}
                  </tbody>
                </table>
              </div>
            )}
      </section>
    </div>
  )
}
