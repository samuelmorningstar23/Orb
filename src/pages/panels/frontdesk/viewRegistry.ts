/**
 * The Front Desk panel's view contract (opd-15, W2-E).
 *
 * The OPD queue and Transfers are built into ../FrontDeskPanel.tsx and never
 * move. Every further desk screen of the registration build (clinic timings,
 * the register view, the appointments day, the day summary, display boards) is
 * its own file in this folder whose name ends in `View.tsx`, found with
 * `import.meta.glob('./frontdesk/*View.tsx')`, so each lands as ONE new file and
 * no two clusters edit the panel.
 *
 * THESE VIEWS EXIST ONLY WHILE THE 'registration' FEATURE IS ON. The panel asks
 * the shell's feature flags (the /api/health 'features' answer) and, with the
 * feature off, shows exactly the two built-in sections it always had.
 *
 * WHAT A VIEW FILE EXPORTS
 *
 *   export default function ClinicSessionsView(props: FrontDeskViewProps) { ... }
 *
 *   // All optional. Plain string and number literals only, so React fast
 *   // refresh (and the lint rule that guards it) accepts them beside the
 *   // component.
 *   export const viewId = 'clinic-sessions'     // else from the file name
 *   export const viewLabel = 'Clinic timings'   // else from the id, sentence case
 *   export const viewOrder = 30                 // else DEFAULT_VIEW_ORDER; lower is first
 *   export const viewPermission = 'registration.read'   // else no extra gate
 *
 * Suggested order, after the built-in OPD queue and Transfers: register 10,
 * appointments 20, clinic timings 30, day summary 40, display boards 50.
 *
 * `viewPermission` HIDES a view; it never grants one. The server refuses every
 * call the role cannot make whatever the tab strip shows, and a permission the
 * client mirror (src/lib/roles.ts) does not know reads as not held.
 *
 * A file that does not default-export a component, or whose id is malformed,
 * clashes with the built-in sections ('opd', 'transfers') or repeats another
 * view's id, is left out with a console warning rather than breaking the desk.
 */
import type { ComponentType } from 'react'

/** What the panel hands every view. */
export interface FrontDeskViewProps {
  /** The caller's department AS THE SERVER DERIVED IT (the OPD queue's
   *  `department`), or null for an account with no department. Never guessed
   *  from the signed-in user object. */
  scopedDepartment: string | null
  /** Switch the desk to another section by id. Unknown ids are ignored. */
  openView: (viewId: string) => void
  /** Call when a request answers `feature_disabled`: the panel drops every
   *  discovered view and shows the built-in sections only. */
  onFeatureOff: () => void
}

export interface FrontDeskViewModule {
  default?: unknown
  viewId?: unknown
  viewLabel?: unknown
  viewOrder?: unknown
  viewPermission?: unknown
}

export interface FrontDeskViewEntry {
  id: string
  label: string
  order: number
  permission: string | null
  Component: ComponentType<FrontDeskViewProps>
  source: string
}

export const DEFAULT_VIEW_ORDER = 500

/** Ids the panel's own sections hold; a view may not take them. */
export const BUILT_IN_SECTION_IDS: readonly string[] = ['opd', 'transfers']

const VIEW_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const VIEW_FILE = /(?:^|\/)([A-Za-z0-9]+)View\.tsx$/

/** `./frontdesk/ClinicSessionsView.tsx` gives `clinic-sessions`. */
export function viewIdFromPath(path: string): string | null {
  const match = VIEW_FILE.exec(path)
  if (!match) return null
  const id = match[1]
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
    .toLowerCase()
  return VIEW_ID.test(id) ? id : null
}

/** `clinic-sessions` gives `Clinic sessions`. */
export function labelFromId(id: string): string {
  const words = id.split('-').filter(Boolean).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function isComponent(value: unknown): value is ComponentType<FrontDeskViewProps> {
  return typeof value === 'function' || (typeof value === 'object' && value !== null
    && '$$typeof' in (value as Record<string, unknown>))
}

function cleanText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** The installed views, valid ones only, in tab order. */
export function discoverFrontDeskViews(
  modules: Readonly<Record<string, unknown>>,
): FrontDeskViewEntry[] {
  const seen = new Set<string>(BUILT_IN_SECTION_IDS)
  const entries: FrontDeskViewEntry[] = []
  for (const source of Object.keys(modules).sort()) {
    const mod = (modules[source] ?? {}) as FrontDeskViewModule
    if (!isComponent(mod.default)) {
      console.warn(`[front desk] ${source} has no default component; left out`)
      continue
    }
    const id = cleanText(mod.viewId) ?? viewIdFromPath(source)
    if (!id || !VIEW_ID.test(id)) {
      console.warn(`[front desk] ${source} has no usable view id; left out`)
      continue
    }
    if (seen.has(id)) {
      console.warn(`[front desk] ${source} repeats the section id "${id}"; left out`)
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
export function visibleFrontDeskViews(
  entries: readonly FrontDeskViewEntry[],
  holds: (permission: string) => boolean,
): FrontDeskViewEntry[] {
  return entries.filter(entry => entry.permission === null || holds(entry.permission))
}
