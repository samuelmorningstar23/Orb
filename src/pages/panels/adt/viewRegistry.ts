/**
 * The Beds panel's view contract (adt-4a).
 *
 * The panel (../BedBoardPanel.tsx) never names a view. It discovers every
 * file in this folder whose name ends in `View.tsx` with `import.meta.glob`,
 * the pattern the shell, the pharmacy store and the laboratory already use,
 * so each Beds screen (board, awaiting a bed, setup, census, movements) lands
 * as ONE new file and no two increments edit the same one.
 *
 * WHAT A VIEW FILE EXPORTS
 *
 *   export default function BoardView(props: AdtViewProps) { ... }
 *
 *   // All optional. Plain string and number literals only, so React fast
 *   // refresh (and the lint rule that guards it) accepts them beside the
 *   // component.
 *   export const viewId = 'board'            // else from the file name
 *   export const viewLabel = 'Board'         // else from the id, sentence case
 *   export const viewOrder = 10              // else DEFAULT_VIEW_ORDER; lower is first
 *   export const viewPermission = 'adt.read' // else no extra gate
 *
 * Suggested order, so the tabs read in the order a bed manager works: board
 * 10, awaiting 20, movements 40, census 60, setup 90.
 *
 * `viewPermission` HIDES a view; it never grants one. The server refuses every
 * call the role cannot make whatever the tab strip shows. A permission the
 * client mirror (src/lib/roles.ts) does not know reads as NOT held, so a view
 * gated on it stays hidden. That is the safe way round.
 *
 * A file that does not default-export a component, or whose id is malformed or
 * already taken, is left out with a console warning rather than breaking the
 * panel.
 */
import type { ComponentType } from 'react'

import type { AdtMeta } from '../../../api/adt'

/** What the panel hands every view. */
export interface AdtViewProps {
  /** GET /api/adt/meta as the panel last read it. */
  meta: AdtMeta
  /** Switch the panel to another view by id. An id that is not installed or
   *  not visible to this user is ignored. */
  openView: (viewId: string) => void
  /** Call when any request answers `module_disabled`: the panel then replaces
   *  every view with the switched-off notice. */
  onModuleOff: () => void
  /** Whether the signed-in user holds a permission (the client mirror). */
  holds: (permission: string) => boolean
}

/** The shape of one `*View.tsx` module as the glob returns it. */
export interface AdtViewModule {
  default?: unknown
  viewId?: unknown
  viewLabel?: unknown
  viewOrder?: unknown
  viewPermission?: unknown
}

/** One installed view, ready for the tab strip. */
export interface AdtViewEntry {
  id: string
  label: string
  order: number
  /** Permission the user must hold for the tab to show, or null for none. */
  permission: string | null
  Component: ComponentType<AdtViewProps>
  /** The file it came from, for warnings and tests. */
  source: string
}

export const DEFAULT_VIEW_ORDER = 500

const VIEW_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const VIEW_FILE = /(?:^|\/)([A-Za-z0-9]+)View\.tsx$/

/** `./adt/BedSetupView.tsx` gives `bed-setup`; null when the path is not a
 *  view file. */
export function viewIdFromPath(path: string): string | null {
  const match = VIEW_FILE.exec(path)
  if (!match) return null
  const id = match[1]
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
    .toLowerCase()
  return VIEW_ID.test(id) ? id : null
}

/** `bed-setup` gives `Bed setup`. Sentence case, as every label is. */
export function labelFromId(id: string): string {
  const words = id.split('-').filter(Boolean).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function isComponent(value: unknown): value is ComponentType<AdtViewProps> {
  // A function component, or a memo / forwardRef / lazy object.
  return typeof value === 'function' || (typeof value === 'object' && value !== null
    && '$$typeof' in (value as Record<string, unknown>))
}

function cleanText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** The installed views, valid ones only, in tab order. */
export function discoverViews(modules: Readonly<Record<string, unknown>>): AdtViewEntry[] {
  const seen = new Set<string>()
  const entries: AdtViewEntry[] = []
  for (const source of Object.keys(modules).sort()) {
    const mod = (modules[source] ?? {}) as AdtViewModule
    if (!isComponent(mod.default)) {
      console.warn(`[beds] ${source} has no default component; left out`)
      continue
    }
    const declared = cleanText(mod.viewId)
    const id = declared ?? viewIdFromPath(source)
    if (!id || !VIEW_ID.test(id)) {
      console.warn(`[beds] ${source} has no usable view id; left out`)
      continue
    }
    if (seen.has(id)) {
      console.warn(`[beds] ${source} repeats the view id "${id}"; left out`)
      continue
    }
    seen.add(id)
    const order = typeof mod.viewOrder === 'number' && Number.isFinite(mod.viewOrder)
      ? mod.viewOrder
      : DEFAULT_VIEW_ORDER
    entries.push({
      id,
      label: cleanText(mod.viewLabel) ?? labelFromId(id),
      order,
      permission: cleanText(mod.viewPermission),
      Component: mod.default,
      source,
    })
  }
  return entries.sort((a, b) => a.order - b.order
    || a.label.localeCompare(b.label)
    || a.id.localeCompare(b.id))
}

/** The views this user may see. */
export function visibleViews(
  entries: readonly AdtViewEntry[],
  holds: (permission: string) => boolean,
): AdtViewEntry[] {
  return entries.filter(entry => entry.permission === null || holds(entry.permission))
}
