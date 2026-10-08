/**
 * Bed board tile action: Record end of stay (adt-9).
 *
 * Opens the End of stay screen for the patient in this bed: LAMA, DAMA,
 * absconded or death, with the name, MRN and bed read back and the MRN typed
 * from the wristband before anything is recorded. Found by the board through
 * ./index.ts; the board is not edited for it.
 *
 * Offered only to a role holding discharge.generate (doctors; the server
 * refuses everyone else), only on a tile with a patient in it, and only where
 * the caller may see who that patient is (actionNeedsOccupant).
 *
 * ONE SCREEN PER STAY. The board draws the open action in one unkeyed place
 * and every tile's buttons stay live while it is open, so pressing this on a
 * second bed re-renders the same component with the new tile. The drawer is
 * keyed by the stay, so the second patient gets a fresh screen: nothing
 * chosen, nothing typed, no result from the first patient, and a new
 * Idempotency-Key. Only the read-back would change otherwise, and the first
 * patient's end type and notes would go into the second patient's record.
 * A board refresh of the same stay keeps the same key and what was typed.
 */
import EndStayDrawer from '../EndStayDrawer'
import type { TileActionProps } from './index'

export const actionId = 'end-stay'
export const actionLabel = 'Record end of stay'
export const actionOrder = 90
export const actionPermission = 'discharge.generate'
export const actionStatuses = 'occupied,occupied_on_leave,occupied_absconded,boarder'
export const actionNeedsOccupant = 1

export default function EndStayAction({ tile, ward, onDone, onClose }: TileActionProps) {
  const occupant = tile.occupant
  if (!occupant) {
    return (
      <div className="es">
        <p className="es__note">Nobody you can see is in this bed, so there is no stay to end here.</p>
        <div className="es__actions">
          <button type="button" className="es__btn" onClick={onClose}>Close</button>
        </div>
      </div>
    )
  }
  return (
    <EndStayDrawer
      key={occupant.admission_id}
      admissionId={occupant.admission_id}
      patient={{
        id: occupant.patient_id,
        name: occupant.name,
        mrn: occupant.mrn,
        ward: ward.code,
        bed: tile.bed_label ?? tile.unit_code,
      }}
      onDone={() => onDone()}
      onClose={onClose}
    />
  )
}
