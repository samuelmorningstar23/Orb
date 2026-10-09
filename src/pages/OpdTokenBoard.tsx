import { useEffect, useState, useSyncExternalStore } from 'react'
import ModuleOffNotice from '../components/ModuleOffNotice'
import {
  DEFAULT_BOARD_POLL_SECONDS,
  boardKeyFromHash,
  readBoard,
  type BoardRead,
  type BoardSnapshot,
} from '../api/opdBoardScreen'
import { formatClinicalTime } from '../lib/datetime'
import {
  currentFeatureFlags,
  currentHealthProbe,
  currentSwitchesSource,
  refreshModuleFlags,
  subscribeModuleFlags,
  type ModuleOffState,
} from '../shellConfig'
import './OpdTokenBoard.css'

/**
 * THE OPD TOKEN BOARD KIOSK, /display/opd (opd-9).
 *
 * The waiting-room screen that calls tokens. Ruling OPD-N7, applied: token,
 * doctor and room only, never a name, UHID or complaint, so the screen can
 * hang where every patient can read it.
 *
 * A board is a DEVICE, not a user:
 *   - it signs nobody in and reads no session, and the route renders whatever
 *     session the browser holds (OpdTokenBoard.route.test.tsx);
 *   - its key comes from the URL FRAGMENT (#key=...), which a browser never
 *     sends to a server or writes into a proxy log, and goes out only in the
 *     X-Orb-Board-Key request header (src/api/opdBoardScreen.ts), never in a
 *     query string or a body; the administrator makes the link on the Front
 *     Desk's Display boards view;
 *   - while the 'opd' feature is off it fetches nothing but the
 *     unauthenticated /api/health and says so.
 *
 * It re-reads every 10 seconds (the board's own answer sets the interval). If
 * a read fails, the last tokens stay on screen with the time they were read
 * and a plain line saying they are not current, so a stale board never looks
 * live. A key that is wrong or switched off clears the tokens at once.
 */

type ScreenState =
  | { kind: 'waiting' }
  | { kind: 'refused' }
  | { kind: 'off' }
  | { kind: 'shown'; snapshot: BoardSnapshot; stale: boolean }
  | { kind: 'unreachable' }

function nextState(prev: ScreenState, read: BoardRead): ScreenState {
  if (read.kind === 'ok') return { kind: 'shown', snapshot: read.snapshot, stale: false }
  if (read.kind === 'refused') return { kind: 'refused' }
  if (read.kind === 'off') return { kind: 'off' }
  return prev.kind === 'shown' ? { ...prev, stale: true } : { kind: 'unreachable' }
}

function useBoardKey(): string | null {
  const [key, setKey] = useState<string | null>(() => boardKeyFromHash(window.location.hash))
  useEffect(() => {
    const onHash = () => setKey(boardKeyFromHash(window.location.hash))
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  return key
}

function Plain({ children }: { children: React.ReactNode }) {
  return (
    <div className="orb-panel-loading opd-board__notice" role="status" aria-live="polite">
      {children}
    </div>
  )
}

function DoctorCard({ doctor }: { doctor: BoardSnapshot['doctors'][number] }) {
  return (
    <li className="opd-board__doctor">
      <div className="opd-board__doctor-head">
        <h2 className="opd-board__doctor-name">{doctor.doctor_name}</h2>
        {doctor.room && <p className="opd-board__room">Room {doctor.room}</p>}
      </div>
      <div className="opd-board__calling">
        <span className="opd-board__label">Now calling</span>
        <span className="opd-board__token opd-board__token--now">
          {doctor.calling != null ? doctor.calling : 'None'}
        </span>
      </div>
      <div className="opd-board__next">
        <span className="opd-board__label">Next</span>
        {doctor.next.length === 0
          ? <span className="opd-board__none">No one waiting</span>
          : (
            <ol className="opd-board__next-list">
              {doctor.next.map(token => <li key={token} className="opd-board__token">{token}</li>)}
            </ol>
          )}
      </div>
    </li>
  )
}

export default function OpdTokenBoard() {
  const features = useSyncExternalStore(subscribeModuleFlags, currentFeatureFlags)
  const probe = useSyncExternalStore(subscribeModuleFlags, currentHealthProbe)
  const source = useSyncExternalStore(subscribeModuleFlags, currentSwitchesSource)
  const key = useBoardKey()
  const [screen, setScreen] = useState<ScreenState>({ kind: 'waiting' })
  const opdOn = features.opd === true

  useEffect(() => { void refreshModuleFlags() }, [])

  /* Read the board only while the server says outpatients is on and the
     address carries a key; then every poll_seconds. */
  useEffect(() => {
    if (!opdOn || !key) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    const tick = async () => {
      const read = await readBoard(key)
      if (!alive) return
      setScreen(prev => nextState(prev, read))
      const seconds = read.kind === 'ok'
        ? Math.max(5, read.snapshot.poll_seconds || DEFAULT_BOARD_POLL_SECONDS)
        : DEFAULT_BOARD_POLL_SECONDS
      if (read.kind !== 'refused' && read.kind !== 'off') {
        timer = setTimeout(() => { void tick() }, seconds * 1000)
      }
    }
    void tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
  }, [opdOn, key])

  /* The switch notice never guesses: 'checking' until the switches are known
     and 'unreachable' when the probe fails. When the page carried the
     switches they are the server's answer already, so 'off' shows at once. */
  const offState: ModuleOffState =
    probe === 'pending' && source !== 'page' ? 'checking' : probe === 'failed' ? 'unreachable' : 'off'

  let body: React.ReactNode
  if (!opdOn || screen.kind === 'off') {
    body = <ModuleOffNotice subject="opd" state={screen.kind === 'off' ? 'off' : offState} />
  } else if (!key) {
    body = (
      <Plain>
        This screen has no board key. Open the board&rsquo;s link from the Front Desk&rsquo;s Display
        boards, on this screen.
      </Plain>
    )
  } else if (screen.kind === 'refused') {
    body = (
      <Plain>
        This screen&rsquo;s board key is not valid. It may have been switched off. Ask an
        administrator for the board&rsquo;s link.
      </Plain>
    )
  } else if (screen.kind === 'waiting') {
    body = <Plain>Loading the tokens…</Plain>
  } else if (screen.kind === 'unreachable') {
    body = <Plain>The token board cannot reach the server. It keeps trying.</Plain>
  } else {
    const snap = screen.snapshot
    body = (
      <>
        <header className="opd-board__head">
          <p className="opd-board__clinic">{snap.board_label || snap.department}</p>
          <p className="opd-board__time">
            {screen.stale
              ? `Not current: last updated ${formatClinicalTime(snap.generated_at)}. Trying again.`
              : `Updated ${formatClinicalTime(snap.generated_at)}`}
          </p>
        </header>
        {snap.doctors.length === 0
          ? <Plain>No tokens are being called in this clinic now.</Plain>
          : (
            <ul className="opd-board__doctors">
              {snap.doctors.map((d, i) => <DoctorCard key={`${d.doctor_name}-${i}`} doctor={d} />)}
            </ul>
          )}
      </>
    )
  }

  return (
    <main className="opd-token-board opd-board">
      <h1 className="opd-board__title">Token board</h1>
      {body}
    </main>
  )
}
