/**
 * Beds, Board view (adt-4a): every bed on the caller's wards, grouped by
 * room, with the status the server derived for it.
 *
 * THE SERVER WRITES EVERY WORD. A tile prints `status_words`, `markers` and
 * the occupant exactly as GET /api/adt/board sent them and is styled by
 * `tone` alone, so a status a later increment adds (leave, reservations)
 * needs no edit here. An occupant outside the caller's department is never
 * named: the server sends no patient field for that tile, and this view has
 * nothing to show but the words 'Occupied' or 'Boarder'.
 *
 * TILE ACTIONS come from src/components/adt/actions/*Action.tsx (assign a
 * bed, change a bed, end of stay and so on, each its own increment). With
 * none installed a tile has no buttons.
 *
 * Polls every 30 seconds. A failed refresh keeps the last board on screen and
 * says when it was read, rather than blanking a board somebody is using.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import { toAdtError } from '../../../api/adt'
import { getBedBoard, type BedBoard, type BedTile, type BoardWard } from '../../../api/adt/board'
import {
  actionsForTile,
  TILE_ACTIONS,
  type TileActionEntry,
} from '../../../components/adt/actions'
import { formatClinicalDateTime, formatClinicalTime } from '../../../lib/datetime'
import type { AdtViewProps } from './viewRegistry'

export const viewId = 'board'
export const viewLabel = 'Board'
export const viewOrder = 10
export const viewPermission = 'adt.read'

const POLL_MS = 30_000

const KNOWN_TONES = new Set(['occupied', 'attention', 'ready', 'progress', 'held', 'off', 'neutral'])

/** Counts that are totals, not statuses. */
const TOTAL_KEYS = new Set(['beds', 'free', 'occupied_total'])

/** "for 45 min", "for 3 h 10 min", "for 2 days". */
function forHowLong(minutes: number | null): string {
  if (minutes === null || minutes === undefined || minutes < 0) return ''
  if (minutes < 60) return `for ${minutes} min`
  if (minutes < 48 * 60) {
    const h = Math.floor(minutes / 60)
    const m = minutes % 60
    return m ? `for ${h} h ${m} min` : `for ${h} h`
  }
  return `for ${Math.floor(minutes / (24 * 60))} days`
}

function toneClass(tone: string): string {
  return `bb-tile--${KNOWN_TONES.has(tone) ? tone : 'neutral'}`
}

/** Tiles grouped by room, in the order the server sent them. */
function byRoom(tiles: readonly BedTile[]): { room: string; tiles: BedTile[] }[] {
  const groups = new Map<string, BedTile[]>()
  for (const tile of tiles) {
    const label = tile.room_name || tile.room || ''
    const list = groups.get(label) ?? []
    list.push(tile)
    groups.set(label, list)
  }
  return [...groups.entries()].map(([room, list]) => ({ room, tiles: list }))
}

type Load =
  | { state: 'loading' }
  | { state: 'ready'; board: BedBoard; stale: string | null }
  | { state: 'error'; message: string }

interface OpenAction {
  entry: TileActionEntry
  tile: BedTile
  ward: BoardWard
}

export default function BoardView({ onModuleOff, holds }: AdtViewProps) {
  const [wardId, setWardId] = useState('')
  const [load, setLoad] = useState<Load>({ state: 'loading' })
  const [notice, setNotice] = useState<string | null>(null)
  const [action, setAction] = useState<OpenAction | null>(null)
  // The newest request wins: a reply to an older one (a slow poll, or the
  // ward before the picker changed) is dropped.
  const latest = useRef(0)

  // State changes only in the reply's callbacks, never synchronously.
  const refresh = useCallback((asked: string) => {
    const mine = ++latest.current
    getBedBoard(asked || null).then(
      (board) => {
        if (mine === latest.current) setLoad({ state: 'ready', board, stale: null })
      },
      (err: unknown) => {
        if (mine !== latest.current) return
        const failure = toAdtError(err)
        if (failure.isModuleDisabled) {
          onModuleOff()
          return
        }
        if (failure.isNotFound && asked) {
          setNotice('That ward is not on your bed board. Showing your wards.')
          setWardId('')
          return
        }
        const message = failure.isForbidden
          ? 'Your role cannot see the bed board.'
          : failure.message
        setLoad((prev) => prev.state === 'ready'
          ? { ...prev, stale: message }
          : { state: 'error', message })
      },
    )
  }, [onModuleOff])

  useEffect(() => {
    refresh(wardId)
    const timer = window.setInterval(() => refresh(wardId), POLL_MS)
    return () => window.clearInterval(timer)
  }, [refresh, wardId])

  const statusOrder = useMemo(() => {
    if (load.state !== 'ready') return [] as string[]
    return Object.keys(load.board.status_words)
  }, [load])

  const closeAction = useCallback(() => setAction(null), [])
  const actionDone = useCallback(() => {
    setAction(null)
    refresh(wardId)
  }, [refresh, wardId])

  if (load.state === 'loading') {
    return <p className="bb-status" role="status">Loading the bed board</p>
  }
  if (load.state === 'error') {
    return (
      <section className="bb-notice bb-notice--error" role="alert">
        <p className="bb-notice__body">{load.message}</p>
        <button type="button" className="bb-btn" onClick={() => refresh(wardId)}>Try again</button>
      </section>
    )
  }

  const { board, stale } = load
  const words = board.status_words

  return (
    <div className="bb-board">
      <div className="bb-toolbar">
        {board.ward_choices.length > 1 && (
          <label className="bb-field">
            <span className="bb-field__label">Ward</span>
            <select
              className="bb-select"
              value={wardId}
              onChange={(e) => { setNotice(null); setWardId(e.target.value) }}
            >
              <option value="">All my wards</option>
              {board.ward_choices.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name && w.name !== w.code ? `${w.code}, ${w.name}` : w.code}
                </option>
              ))}
            </select>
          </label>
        )}
        <p className="bb-updated">
          Updated {formatClinicalTime(board.generated_at)}
        </p>
      </div>

      {notice && <p className="bb-note" role="status">{notice}</p>}
      {stale && (
        <p className="bb-note bb-note--warn" role="alert">
          Could not refresh: {stale} The board below was read at {formatClinicalTime(board.generated_at)}.
        </p>
      )}

      {board.wards.length === 0 && (
        <section className="bb-empty" aria-labelledby="bb-no-wards">
          <h2 id="bb-no-wards" className="bb-empty__title">No wards on your board</h2>
          <p className="bb-empty__body">
            No ward in bed setup belongs to your department yet. A bed manager sets wards
            and beds up under bed setup.
          </p>
        </section>
      )}

      {action && (
        <section className="bb-action" aria-label={action.entry.label}>
          <action.entry.Component
            tile={action.tile}
            ward={action.ward}
            onDone={actionDone}
            onClose={closeAction}
          />
        </section>
      )}

      {board.wards.map((ward) => (
        <section key={ward.id} className="bb-ward" aria-labelledby={`bb-ward-${ward.id}`}>
          <header className="bb-ward__head">
            <h2 id={`bb-ward-${ward.id}`} className="bb-ward__title">
              {ward.code}
              {ward.name && ward.name !== ward.code ? <span className="bb-ward__name">{ward.name}</span> : null}
            </h2>
            <p className="bb-ward__dept">
              {ward.department}
              {!ward.is_active ? ', switched off in bed setup' : ''}
            </p>
          </header>

          <ul className="bb-counts" aria-label={`Bed counts for ${ward.code}`}>
            <li className="bb-count"><span className="bb-count__n">{ward.counts.beds ?? 0}</span> Beds</li>
            <li className="bb-count"><span className="bb-count__n">{ward.counts.free ?? 0}</span> Free now</li>
            {[...statusOrder, ...Object.keys(ward.counts).filter((k) => !statusOrder.includes(k))]
              .filter((k) => !TOTAL_KEYS.has(k) && (ward.counts[k] ?? 0) > 0)
              .map((k) => (
                <li key={k} className="bb-count">
                  <span className="bb-count__n">{ward.counts[k]}</span> {words[k] ?? k.replace(/_/g, ' ')}
                </li>
              ))}
          </ul>

          {!ward.cleaning_tracked && (
            <p className="bb-note">Housekeeping does not track cleaning on this ward.</p>
          )}

          {ward.tiles.length === 0 && (
            <p className="bb-note">No beds are set up on this ward yet.</p>
          )}

          {byRoom(ward.tiles).map(({ room, tiles }) => (
            <div key={room || 'no-room'} className="bb-room">
              {room && <h3 className="bb-room__title">{room}</h3>}
              <ul className="bb-grid">
                {tiles.map((tile) => (
                  <BedTileCard
                    key={tile.bed_id}
                    tile={tile}
                    actions={actionsForTile(TILE_ACTIONS, tile, holds)}
                    onAction={(entry) => setAction({ entry, tile, ward })}
                  />
                ))}
              </ul>
            </div>
          ))}
        </section>
      ))}
    </div>
  )
}

interface BedTileCardProps {
  tile: BedTile
  actions: TileActionEntry[]
  onAction: (entry: TileActionEntry) => void
}

function BedTileCard({ tile, actions, onAction }: BedTileCardProps) {
  const said = tile.status_words.toLowerCase()
  const markers = tile.markers.filter((m) => !said.includes(m.toLowerCase()))
  const howLong = forHowLong(tile.minutes_in_state)
  const occupant = tile.occupant
  return (
    <li className={`bb-tile ${toneClass(String(tile.tone))}`} data-status={tile.status}>
      <div className="bb-tile__head">
        <span className="bb-tile__label">{tile.bed_label ?? tile.unit_code}</span>
        <span className="bb-tile__chips">
          {tile.isolation && <span className="bb-chip">Isolation</span>}
          {tile.temporary && <span className="bb-chip">Temporary bed</span>}
          {tile.bed_class && <span className="bb-chip">{tile.bed_class}</span>}
        </span>
      </div>
      <p className="bb-tile__status">{tile.status_words}</p>
      {occupant && (
        <div className="bb-tile__who">
          <span className="bb-tile__name">{occupant.name ?? 'Name not recorded'}</span>
          {occupant.mrn && <span className="bb-tile__mrn">{occupant.mrn}</span>}
          {occupant.boarder_from && (
            <span className="bb-tile__boarder">Boarder from {occupant.boarder_from}</span>
          )}
        </div>
      )}
      {markers.length > 0 && (
        <ul className="bb-tile__markers">
          {markers.map((m) => <li key={m}>{m}</li>)}
        </ul>
      )}
      {howLong && (
        <p className="bb-tile__since" title={tile.since ? formatClinicalDateTime(tile.since) : undefined}>
          {howLong}
        </p>
      )}
      {actions.length > 0 && (
        <div className="bb-tile__actions">
          {actions.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className="bb-btn bb-btn--small"
              onClick={() => onAction(entry)}
            >
              {entry.label}
            </button>
          ))}
        </div>
      )}
    </li>
  )
}
