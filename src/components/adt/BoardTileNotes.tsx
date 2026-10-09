/**
 * The lines a bed board tile adds under its status (adt-10, adt-12).
 *
 *  - A patient off the ward: 'Not on ward since 02:05 PM (on leave)', from the
 *    occupant the server sent (only inside the caller's department).
 *  - A bed held by a reservation: when it expires ('expires 03:40 PM'), and
 *    who it is held for when the caller may see that patient. Outside the
 *    caller's department it says only that the bed is held for a patient on
 *    another team's list; it never names them.
 *
 * The status words themselves ('Reserved for transfer') are the server's and
 * are printed by the board. Times are local, through src/lib/datetime.
 */
import type { BedTile } from '../../api/adt/board'
import { notOnWardWords } from '../../api/adt/presence'
import { expiryWords, reservationOf } from '../../api/adt/reservations'
import './NotOnWard.css'

export default function BoardTileNotes({ tile }: { tile: BedTile }) {
  const away = tile.occupant ? notOnWardWords(tile.occupant) : null
  const held = reservationOf(tile)
  if (!away && !held) return null
  const expires = held ? expiryWords(held.expires_at) : ''
  return (
    <>
      {away && <p className="adt-tile-note" data-testid="tile-not-on-ward">{away}</p>}
      {held && expires && (
        <p className="adt-tile-note" data-testid="tile-reservation-expiry">
          {expires.charAt(0).toUpperCase() + expires.slice(1)}
        </p>
      )}
      {held?.patient && (
        <p className="adt-tile-note adt-tile-note--who">
          For {held.patient.name ?? 'a patient whose name is not recorded'}
          {held.patient.mrn ? `, ${held.patient.mrn}` : ''}
        </p>
      )}
      {held && !held.patient && (
        <p className="adt-tile-note">
          {held.patient_hidden ? 'Held for a patient outside your department' : 'No patient named yet'}
        </p>
      )}
    </>
  )
}
