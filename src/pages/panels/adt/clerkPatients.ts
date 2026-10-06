/**
 * The ward clerk's patient list for the Beds, Observations view
 * (ObservationsView.tsx, Orb Ward Record, 6 October 2026).
 */
/* SELF-CONTAINED ON PURPOSE. The Beds panel discovers ObservationsView.tsx by
   its name (viewRegistry.ts, `*View.tsx`) and hands it AdtViewProps; its two
   reads are GET /api/adt/board and GET /api/adt/awaiting-bed. Only the fields
   this view uses are typed here, structurally, so the view builds on its own
   and needs nothing from the panel but the props it is given. */

/** The part of AdtViewProps this view uses (viewRegistry.ts). */
export interface ObservationsViewProps {
  onModuleOff: () => void
}

interface BoardTile {
  bed_label: string | null
  unit_code: string
  occupant: { patient_id: string; name: string | null; mrn: string | null } | null
  occupant_hidden: boolean
}

/** GET /api/adt/board, the fields read here. */
export interface ClerkBoard {
  wards: { code: string; name: string | null; tiles: BoardTile[] }[]
}

interface ClerkAwaitingEntry {
  patient_id: string
  name: string | null
  mrn: string | null
  ward: string | null
  bed: string | null
}

/** GET /api/adt/awaiting-bed, the fields read here. */
export interface ClerkAwaiting {
  awaiting: ClerkAwaitingEntry[]
  unlinked: ClerkAwaitingEntry[]
}

/** What a refused read means here. */
export function failureOf(err: unknown): { moduleOff: boolean; message: string } {
  const raw = (err ?? {}) as { status?: unknown; message?: unknown; detail?: unknown; permissions?: unknown }
  const detail = (raw.detail ?? raw.permissions ?? null) as { error?: unknown } | null
  const status = typeof raw.status === 'number' ? raw.status : 0
  if (detail && typeof detail === 'object' && detail.error === 'module_disabled') {
    return { moduleOff: true, message: '' }
  }
  if (status === 403) return { moduleOff: false, message: 'Your role cannot see this list.' }
  if (status === 0) {
    return { moduleOff: false, message: 'Could not reach the ward list. Check the connection and try again.' }
  }
  return {
    moduleOff: false,
    message: typeof raw.message === 'string' && raw.message.trim()
      ? raw.message
      : 'The ward list could not be loaded.',
  }
}


/** One patient the clerk can key a round for. */
export interface ClerkPatientRow {
  patientId: string
  name: string
  mrn: string | null
  ward: string | null
  bed: string | null
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' })

/** Every patient on the two lists, once each, by ward then bed then name. */
export function mergePatients(
  board: ClerkBoard,
  awaiting: ClerkAwaiting | null,
): ClerkPatientRow[] {
  const byId = new Map<string, ClerkPatientRow>()
  for (const ward of board.wards) {
    for (const tile of ward.tiles) {
      const who = tile.occupant
      if (!who || tile.occupant_hidden || byId.has(who.patient_id)) continue
      byId.set(who.patient_id, {
        patientId: who.patient_id,
        name: who.name || 'Name not recorded',
        mrn: who.mrn,
        ward: ward.name || ward.code,
        bed: tile.bed_label || tile.unit_code,
      })
    }
  }
  for (const entry of [...(awaiting?.unlinked ?? []), ...(awaiting?.awaiting ?? [])]) {
    if (byId.has(entry.patient_id)) continue
    byId.set(entry.patient_id, {
      patientId: entry.patient_id,
      name: entry.name || 'Name not recorded',
      mrn: entry.mrn,
      ward: entry.ward,
      bed: entry.bed,
    })
  }
  return Array.from(byId.values()).sort((a, b) =>
    collator.compare(a.ward ?? '\uffff', b.ward ?? '\uffff')
    || collator.compare(a.bed ?? '\uffff', b.bed ?? '\uffff')
    || collator.compare(a.name, b.name))
}
