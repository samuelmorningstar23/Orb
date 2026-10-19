/**
 * Bed board tile actions (adt-4a): the contract, and every action installed.
 *
 * The board (src/pages/panels/adt/BoardView.tsx) never names an action. Every
 * file in this folder whose name ends in `Action.tsx` is found with
 * `import.meta.glob`, so assigning a bed, changing a bed, recording leave,
 * reserving or ending a stay each lands as ONE new file (adt-5, adt-6a,
 * adt-9, adt-10, adt-12) and the board is never edited for them.
 *
 * WHAT AN ACTION FILE EXPORTS
 *
 *   export default function ChangeBedAction(props: TileActionProps) { ... }
 *
 *   // All optional. Plain string and number literals only, so React fast
 *   // refresh (and the lint rule that guards it) accepts them beside the
 *   // component.
 *   export const actionId = 'change-bed'         // else from the file name
 *   export const actionLabel = 'Change bed'      // else from the id, sentence case
 *   export const actionOrder = 20                // else DEFAULT_ACTION_ORDER; lower is first
 *   export const actionPermission = 'adt.bed'    // else no extra gate
 *   export const actionStatuses = 'occupied,occupied_on_leave'
 *                                                // comma list of tile statuses; '*' or absent
 *                                                // means every status
 *   export const actionNeedsOccupant = 1         // 1: only on a tile whose occupant the
 *                                                // caller may see
 *   export const actionMarker = 'Admission assessment due'
 *                                                // only on a tile whose markers include
 *                                                // these exact words (adt-17b)
 *
 * `actionPermission` HIDES an action; it never grants one. The server refuses
 * every write the role cannot make. A permission the client mirror does not
 * know reads as NOT held, so an action gated on it stays hidden. The Beds
 * panel's check also answers 'role:<role>' from the signed-in role (adt-17b:
 * the ward clerk's own lanes); anything else asking it reads it as not held.
 *
 * A tile whose occupant is outside the caller's department carries no
 * patient field, so an action that needs one must say actionNeedsOccupant = 1
 * and is then never offered on that tile.
 */
import type { ComponentType } from 'react'

import type { BedTile, BoardWard } from '../../../api/adt/board'

/** What the board hands an action. */
export interface TileActionProps {
  tile: BedTile
  ward: BoardWard
  /** The action finished and wrote something: the board reloads. */
  onDone: () => void
  /** The action was closed without writing anything. */
  onClose: () => void
}

/** The shape of one `*Action.tsx` module as the glob returns it. */
export interface TileActionModule {
  default?: unknown
  actionId?: unknown
  actionLabel?: unknown
  actionOrder?: unknown
  actionPermission?: unknown
  actionStatuses?: unknown
  actionNeedsOccupant?: unknown
  actionMarker?: unknown
}

/** One installed action. */
export interface TileActionEntry {
  id: string
  label: string
  order: number
  permission: string | null
  /** The tile statuses it applies to, or null for every status. */
  statuses: readonly string[] | null
  needsOccupant: boolean
  /** Only on a tile whose markers include these words (adt-17b); absent or
   *  null for every tile. */
  marker?: string | null
  Component: ComponentType<TileActionProps>
  source: string
}

export const DEFAULT_ACTION_ORDER = 500

const ACTION_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const ACTION_FILE = /(?:^|\/)([A-Za-z0-9]+)Action\.tsx$/

/** `./ChangeBedAction.tsx` gives `change-bed`; null when not an action file. */
export function actionIdFromPath(path: string): string | null {
  const match = ACTION_FILE.exec(path)
  if (!match) return null
  const id = match[1]
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
    .toLowerCase()
  return ACTION_ID.test(id) ? id : null
}

function labelFromId(id: string): string {
  const words = id.split('-').filter(Boolean).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function isComponent(value: unknown): value is ComponentType<TileActionProps> {
  return typeof value === 'function' || (typeof value === 'object' && value !== null
    && '$$typeof' in (value as Record<string, unknown>))
}

function cleanText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function statusList(value: unknown): readonly string[] | null {
  const text = cleanText(value)
  if (!text || text === '*') return null
  const list = text.split(',').map(s => s.trim()).filter(Boolean)
  return list.length ? list : null
}

/** The installed actions, valid ones only, in menu order. */
export function discoverTileActions(modules: Readonly<Record<string, unknown>>): TileActionEntry[] {
  const seen = new Set<string>()
  const entries: TileActionEntry[] = []
  for (const source of Object.keys(modules).sort()) {
    const mod = (modules[source] ?? {}) as TileActionModule
    if (!isComponent(mod.default)) {
      console.warn(`[beds] ${source} has no default component; left out`)
      continue
    }
    const id = cleanText(mod.actionId) ?? actionIdFromPath(source)
    if (!id || !ACTION_ID.test(id)) {
      console.warn(`[beds] ${source} has no usable action id; left out`)
      continue
    }
    if (seen.has(id)) {
      console.warn(`[beds] ${source} repeats the action id "${id}"; left out`)
      continue
    }
    seen.add(id)
    entries.push({
      id,
      label: cleanText(mod.actionLabel) ?? labelFromId(id),
      order: typeof mod.actionOrder === 'number' && Number.isFinite(mod.actionOrder)
        ? mod.actionOrder : DEFAULT_ACTION_ORDER,
      permission: cleanText(mod.actionPermission),
      statuses: statusList(mod.actionStatuses),
      needsOccupant: mod.actionNeedsOccupant === 1 || mod.actionNeedsOccupant === true,
      marker: cleanText(mod.actionMarker),
      Component: mod.default,
      source,
    })
  }
  return entries.sort((a, b) => a.order - b.order
    || a.label.localeCompare(b.label)
    || a.id.localeCompare(b.id))
}

/** The actions this user may take on this tile. */
export function actionsForTile(
  entries: readonly TileActionEntry[],
  tile: BedTile,
  holds: (permission: string) => boolean,
): TileActionEntry[] {
  return entries.filter(entry =>
    (entry.permission === null || holds(entry.permission))
    && (entry.statuses === null || entry.statuses.includes(tile.status))
    && (!entry.needsOccupant || tile.occupant !== null)
    && (!entry.marker || (tile.markers ?? []).includes(entry.marker)))
}

/** Every action installed in this build, read once (the glob is compile-time). */
export const TILE_ACTIONS: readonly TileActionEntry[] = discoverTileActions(
  import.meta.glob('./*Action.tsx', { eager: true }),
)
