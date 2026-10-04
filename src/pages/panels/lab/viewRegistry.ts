/**
 * The Laboratory panel's view contract (lis-1).
 *
 * The panel (../LabPanel.tsx) never names a view. It discovers every file in
 * this folder whose name ends in `View.tsx` with `import.meta.glob`, the
 * pattern the shell and the pharmacy store already use, so each laboratory
 * screen (collect, receive, worklists, results, validate, critical, reports,
 * turnaround times, exceptions, reconcile, setup, analysers) lands as ONE new
 * file and no two increments edit the same one.
 *
 * WHAT A VIEW FILE EXPORTS
 *
 *   export default function SetupView(props: LabViewProps) { ... }
 *
 *   // All optional. Plain string and number literals only, so React fast
 *   // refresh (and the lint rule that guards it) accepts them beside the
 *   // component.
 *   export const viewId = 'setup'             // else from the file name
 *   export const viewLabel = 'Setup'          // else from the id, sentence case
 *   export const viewOrder = 90               // else DEFAULT_VIEW_ORDER; lower is first
 *   export const viewPermission = 'lab.master'   // else no extra gate
 *   export const viewWhenDraining = 'hide'    // else 'show'
 *
 * Suggested order, so the tabs read in the order a laboratory works: collect
 * 10, receive 20, worklists 30, results 40, validate 50, critical 60, reports
 * 70, turnaround 80, exceptions 85, reconcile 88, setup 90, analysers 95.
 *
 * DRAINING. While the module is switched off with open work (ruling LIS-R21)
 * the panel still opens for laboratory staff to finish, cancel or release
 * that work. A view that only STARTS new work (collecting a new specimen,
 * analyser set-up) exports `viewWhenDraining = 'hide'` and is left out of the
 * tab strip then; the server refuses its calls in that state anyway.
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

import type { LabMeta, LabModuleState } from '../../../api/lab'

/** What the panel hands every view. */
export interface LabViewProps {
  /** GET /api/lab/meta as the panel last read it. */
  meta: LabMeta
  /** 'on', or 'draining' while the module is switched off with open work. */
  state: Exclude<LabModuleState, 'off'>
  /** Switch the panel to another view by id. An id that is not installed or
   *  not visible to this user is ignored. */
  openView: (viewId: string) => void
  /** Call when any request answers `module_disabled`: the panel then replaces
   *  every view with the switched-off notice. A draining refusal of new work
   *  also answers module_disabled; a view that can meet one should say so in
   *  its own words rather than call this. */
  onModuleOff: () => void
}

/** The shape of one `*View.tsx` module as the glob returns it. */
export interface LabViewModule {
  default?: unknown
  viewId?: unknown
  viewLabel?: unknown
  viewOrder?: unknown
  viewPermission?: unknown
  viewWhenDraining?: unknown
}

/** One installed view, ready for the tab strip. */
export interface LabViewEntry {
  id: string
  label: string
  order: number
  /** Permission the user must hold for the tab to show, or null for none. */
  permission: string | null
  /** Shown while the module drains (finishing work) or hidden (new work). */
  whenDraining: 'show' | 'hide'
  Component: ComponentType<LabViewProps>
  /** The file it came from, for warnings and tests. */
  source: string
}

export const DEFAULT_VIEW_ORDER = 500

const VIEW_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/
const VIEW_FILE = /(?:^|\/)([A-Za-z0-9]+)View\.tsx$/

/** `./lab/CriticalResultsView.tsx` gives `critical-results`; null when the
 *  path is not a view file. */
export function viewIdFromPath(path: string): string | null {
  const match = VIEW_FILE.exec(path)
  if (!match) return null
  const id = match[1]
    .replace(/([a-z0-9])([A-Z])/g, '$1-$2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1-$2')
    .toLowerCase()
  return VIEW_ID.test(id) ? id : null
}

/** `critical-results` gives `Critical results`. Sentence case, as every label is. */
export function labelFromId(id: string): string {
  const words = id.split('-').filter(Boolean).join(' ')
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : id
}

function isComponent(value: unknown): value is ComponentType<LabViewProps> {
  // A function component, or a memo / forwardRef / lazy object.
  return typeof value === 'function' || (typeof value === 'object' && value !== null
    && '$$typeof' in (value as Record<string, unknown>))
}

function cleanText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/** The installed views, valid ones only, in tab order. */
export function discoverViews(modules: Readonly<Record<string, unknown>>): LabViewEntry[] {
  const seen = new Set<string>()
  const entries: LabViewEntry[] = []
  for (const source of Object.keys(modules).sort()) {
    const mod = (modules[source] ?? {}) as LabViewModule
    if (!isComponent(mod.default)) {
      console.warn(`[laboratory] ${source} has no default component; left out`)
      continue
    }
    const declared = cleanText(mod.viewId)
    const id = declared ?? viewIdFromPath(source)
    if (!id || !VIEW_ID.test(id)) {
      console.warn(`[laboratory] ${source} has no usable view id; left out`)
      continue
    }
    if (seen.has(id)) {
      console.warn(`[laboratory] ${source} repeats the view id "${id}"; left out`)
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
      whenDraining: mod.viewWhenDraining === 'hide' ? 'hide' : 'show',
      Component: mod.default,
      source,
    })
  }
  return entries.sort((a, b) => a.order - b.order
    || a.label.localeCompare(b.label)
    || a.id.localeCompare(b.id))
}

/** The views this user may see in this module state. */
export function visibleViews(
  entries: readonly LabViewEntry[],
  holds: (permission: string) => boolean,
  state: Exclude<LabModuleState, 'off'> = 'on',
): LabViewEntry[] {
  return entries.filter(entry =>
    (entry.permission === null || holds(entry.permission))
    && (state === 'on' || entry.whenDraining === 'show'))
}
