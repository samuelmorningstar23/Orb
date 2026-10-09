/**
 * Bed board tile action: Leave or absconded (adt-10).
 *
 * Opens the screen that records a patient going on leave, coming back, going
 * without leave or coming back after absconding. The bed stays the
 * patient's. Found by the board through ./index.ts; the board is not edited
 * for it.
 *
 * Only on a tile with a patient in it the caller may see
 * (actionNeedsOccupant). No actionPermission: an action file names one
 * permission, and two hold this (adt.presence, the administrator; and
 * patient.write, the doctors; ruling ADT-S7 held). A role holding neither
 * (the ward clerk) is told who records it, and the server refuses it anyway.
 *
 * ONE SCREEN PER STAY: the drawer is keyed by the admission, so pressing this
 * on a second bed opens a fresh screen with a new Idempotency-Key.
 */
import { canRecordPresence, presenceOf } from '../../../api/adt/presence'
import PresenceDrawer from '../PresenceDrawer'
import type { TileActionProps } from './index'

export const actionId = 'presence'
export const actionLabel = 'Leave or absconded'
export const actionOrder = 30
export const actionStatuses = 'occupied,occupied_on_leave,occupied_absconded,boarder'
export const actionNeedsOccupant = 1

export default function PresenceAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  if (!occupant || !canRecordPresence()) {
    return (
      <div className="es">
        <p className="es__note">
          {occupant
            ? 'Leave and absconding are recorded by a doctor or the administrator.'
            : 'Nobody you can see is in this bed, so there is nothing to record here.'}
        </p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }
  const presence = presenceOf(occupant)
  return (
    <PresenceDrawer
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      patient={{ name: occupant.name, mrn: occupant.mrn, ward: ward.code,
                 bed: tile.bed_label ?? tile.unit_code }}
      presence={presence?.status ?? null}
      presenceSince={presence?.since ?? null}
      onDone={() => onDone()}
      onClose={onClose}
    />
  )
}
