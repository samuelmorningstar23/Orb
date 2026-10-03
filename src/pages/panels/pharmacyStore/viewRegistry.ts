/**
 * The Pharmacy store panel's view contract.
 *
 * The panel (../PharmacyStorePanel.tsx) never names a view. It discovers every
 * file in this folder whose name ends in `View.tsx` with `import.meta.glob`, the
 * pattern the shell already uses for module panels, so each area of the store
 * (masters, purchasing, receipts, stock, transfers, expiry, counts, NDPS, vendor
 * returns, ward stock) lands as ONE new file and no two areas edit the same one.
 *
 * WHAT A VIEW FILE EXPORTS
 *
 *   export default function MastersView(props: PharmacyStoreViewProps) { ... }
 *
 *   // All optional. Plain string and number literals only, so React fast
 *   // refresh (and the lint rule that guards it) accepts them beside the
 *   // component.
 *   export const viewId = 'masters'          // else from the file name
 *   export const viewLabel = 'Masters'       // else from the id, sentence case
 *   export const viewOrder = 90              // else DEFAULT_VIEW_ORDER; lower is first
 *   export const viewPermission = 'pharmacy_store.read'   // else no extra gate
 *
 * Suggested order, so the tabs read in the order a pharmacy works:
 * overview 10, purchasing 20, receipts 30, stock 40, transfers 50, expiry 60,
 * counts 70, ndps 80, vendor-returns 85, masters 90, ward stock and crash carts
 * 100 and up.
 *
 * `viewPermission` HIDES a view; it never grants one. The server refuses every
 * call the role cannot make whatever the tab strip shows. A permission the
 * client mirror (src/lib/roles.ts) does not know reads as NOT held, so a view
 * gated on it stays hidden until the mirror carries it. That is the safe way
 * round: a missing mirror entry hides a screen, it never shows one.
 *
 * A file that does not default-export a component, or whose id is malformed or
 * already taken, is left out with a console warning rather than breaking the
 * panel.
 */
import type { ComponentType } from 'react'

import type { PharmacyStoreMeta } from '../../../api/pharmacyStore'

/** What the panel hands every view. */
export interface PharmacyStoreViewProps {
  /** GET /api/pharmacy-store/meta as the panel last read it. */
  meta: PharmacyStoreMeta
  /** Switch the panel to another view by id (an overview tile, a link). An id
   *  that is not installed or not visible to this user is ignored. */
  openView: (viewId: string) => void
  /** Call when any request answers `module_disabled` (the site switched the
   *  module off while the screen was open). The panel then replaces every view
   *  with the switched-off notice, so the view never has to draw that state
   *  itself and never shows it as an empty list. */
  onModuleOff: () => void
}

/** The shape of one `*View.tsx` module as the glob returns it. */
export interface PharmacyStoreViewModule {
  default?: unknown
  viewId?: unknown
  viewLabel?: unknown
  viewOrder?: unknown
  viewPermission?: unknown
}

/** One installed view, ready for the tab strip. */
export interface PharmacyStoreViewEntry {
  id: string
  label: string
  order: number
  /** Permission the user must hold for the tab to show, or null for none. */
  permission: string | null
  Component: ComponentType<PharmacyStoreViewProps>
  /** The file it came from, for warnings and tests. */
  source: string
}

export const DEFAULT_VIEW_ORDER = 500

const VIEW_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const VIEW_FILE = /(?:^|\/)([A-Za-z0-9]+)View\.tsx$/

/** `./pharmacyStore/VendorReturnsView.tsx` gives `vendor-returns`; null when
 *  the path is not a view file. */
export function viewIdFromPath(path: string): string | null {
  const match = VIEW_FILE.exec(path)
  if (!match) return null
  const id = match[1]
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
    .toLowerCase()
  return VIEW_ID.test(id) ? id : null
}

/** `vendor-returns` gives `Vendor returns`. Sentence case, as every label is. */
export function labelFromId(id: string): string {
  const words = id.split('-').filter(Boolean).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function isComponent(value: unknown): value is ComponentType<PharmacyStoreViewProps> {
  // A function component, or a memo / forwardRef / lazy object.
  return typeof value === 'function' || (typeof value === 'object' && value !== null
    && '$$typeof' in (value as Record<string, unknown>))
}

function cleanText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** The installed views, valid ones only, in tab order. */
export function discoverViews(
  modules: Readonly<Record<string, unknown>>,
): PharmacyStoreViewEntry[] {
  const seen = new Set<string>()
  const entries: PharmacyStoreViewEntry[] = []
  for (const source of Object.keys(modules).sort()) {
    const mod = (modules[source] ?? {}) as PharmacyStoreViewModule
    if (!isComponent(mod.default)) {
      console.warn(`[pharmacy store] ${source} has no default component; left out`)
      continue
    }
    const declared = cleanText(mod.viewId)
    const id = declared ?? viewIdFromPath(source)
    if (!id || !VIEW_ID.test(id)) {
      console.warn(`[pharmacy store] ${source} has no usable view id; left out`)
      continue
    }
    if (seen.has(id)) {
      console.warn(`[pharmacy store] ${source} repeats the view id "${id}"; left out`)
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

/** The views this user may see: those with no permission, and those whose
 *  permission `holds` confirms. */
export function visibleViews(
  entries: readonly PharmacyStoreViewEntry[],
  holds: (permission: string) => boolean,
): PharmacyStoreViewEntry[] {
  return entries.filter(entry => entry.permission === null || holds(entry.permission))
}
