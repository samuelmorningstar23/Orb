/**
 * Bed board tile action: Move patient (adt-6a).
 *
 * Opens the Move screen for the patient in this bed: another bed in the same
 * department (adt.bed, the ward clerk, and doctors through patient.write), or
 * a transfer to another department (adt.transfer, and doctors). Found by the
 * board through ./index.ts; the board is not edited for it.
 *
 * No actionPermission: every role that sees the bed board can make one of
 * the two moves (the administrator both, a doctor both, the ward clerk a bed
 * change), and the action file can name only one permission. The screen
 * offers only the moves the role holds, and the server refuses the rest.
 * Only on a tile with a patient in it the caller may see (actionNeedsOccupant).
 *
 * ONE SCREEN PER STAY, as for End of stay: the drawer is keyed by the
 * admission, so pressing Move on a second bed opens a fresh screen with a
 * new Idempotency-Key, never the first patient's choices.
 */
import { ADT_BED, ADT_TRANSFER, PATIENT_WRITE, hasPermission } from '../../../lib/roles'
import MoveDrawer from '../MoveDrawer'
import type { TileActionProps } from './index'

export const actionId = 'move'
export const actionLabel = 'Move patient'
export const actionOrder = 20
export const actionStatuses = 'occupied,occupied_on_leave,occupied_absconded,boarder'
export const actionNeedsOccupant = 1

export default function MoveAction({ tile, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  if (!occupant) {
    return (
      <div className="es">
        <p className="es__note">Nobody you can see is in this bed, so there is nobody to move from here.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }
  return (
    <MoveDrawer
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      patient={{ name: occupant.name, mrn: occupant.mrn }}
      canTransfer={hasPermission(ADT_TRANSFER) || hasPermission(PATIENT_WRITE)}
      canChangeBed={hasPermission(ADT_BED) || hasPermission(PATIENT_WRITE)}
      onDone={() => onDone()}
      onClose={onClose}
    />
  )
}
