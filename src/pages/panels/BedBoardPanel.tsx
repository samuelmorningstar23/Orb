/**
 * Orb, Beds: the shell for beds and admissions (adt-4a).
 *
 * AN OPT-IN MODULE. The server switch is ORB_ADT and it is OFF unless a site
 * turns it on; the shell shows this tab only when /api/health says the module
 * is on AND this file is in the build (creating it is what installs the tab).
 * Where a patient lies is still typed at admission and shown on every board
 * either way: this module adds the bed register and the board around it.
 *
 * THIS FILE NAMES NO VIEW. Every Beds screen is its own file in ./adt/,
 * found by `import.meta.glob('./adt/*View.tsx')`; the contract a view file
 * follows is in ./adt/viewRegistry.ts. Each later increment (bed setup,
 * movements, census) therefore lands as one new file and never edits this
 * one. Tile actions on the board are found the same way, from
 * src/components/adt/actions/*Action.tsx.
 *
 * WHAT IT SHOWS, IN ORDER OF PRECEDENCE
 *   1. While GET /api/adt/meta is in flight: a loading line.
 *   2. 404 module_disabled: Beds is switched off at this site, said in words
 *      and never drawn as an empty board (an empty board is a statement about
 *      the beds). A view that meets the same answer later calls `onModuleOff`
 *      and the panel shows the same notice.
 *   3. No reply: the server could not be reached, with a way to try again.
 *   4. Otherwise the views this user may see, as tabs. With none open to
 *      this role, a sentence saying so.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { getAdtMeta, toAdtError, type AdtMeta } from '../../api/adt'
import ModuleOffNotice from '../../components/ModuleOffNotice'
import { hasPermission, type Permission } from '../../lib/roles'
import { discoverViews, visibleViews, type AdtViewEntry } from './adt/viewRegistry'
import './BedBoardPanel.css'

/** Every installed view, read once at module load (the glob is compile-time). */
const INSTALLED_VIEWS: readonly AdtViewEntry[] = discoverViews(
  import.meta.glob('./adt/*View.tsx', { eager: true }),
)

/** The client permission mirror. A permission it does not list reads as not
 *  held, so a view gated on it stays hidden rather than shown. */
function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

type Probe =
  | { state: 'checking' }
  | { state: 'ready'; meta: AdtMeta }
  | { state: 'off' }
  | { state: 'unreachable'; message: string }

interface BedBoardPanelProps {
  /** Tests only: the views to offer. The shell mounts the panel without props,
   *  which means every view found in ./adt/. */
  views?: readonly AdtViewEntry[]
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
}

export default function BedBoardPanel({ views = INSTALLED_VIEWS, holds = roleHolds }: BedBoardPanelProps) {
  const [probe, setProbe] = useState<Probe>({ state: 'checking' })
  const [attempt, setAttempt] = useState(0)
  const [chosenId, setChosenId] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getAdtMeta().then(
      (meta) => {
        if (live) setProbe({ state: 'ready', meta: meta ?? {} })
      },
      (err: unknown) => {
        if (!live) return
        const failure = toAdtError(err)
        setProbe(failure.isModuleDisabled
          ? { state: 'off' }
          : { state: 'unreachable', message: failure.message })
      },
    )
    return () => { live = false }
  }, [attempt])

  const retry = useCallback(() => {
    setProbe({ state: 'checking' })
    setAttempt((n) => n + 1)
  }, [])

  const shown = useMemo(() => visibleViews(views, holds), [views, holds])
  const active = shown.find((view) => view.id === chosenId) ?? shown[0] ?? null

  const openView = useCallback((viewId: string) => {
    if (shown.some((view) => view.id === viewId)) setChosenId(viewId)
  }, [shown])

  const onModuleOff = useCallback(() => setProbe({ state: 'off' }), [])

  return (
    <div className="bb-panel">
      <header className="bb-head">
        <h1 className="bb-title">Beds</h1>
        <p className="bb-sub">Where every patient lies, and which beds are ready for the next one</p>
      </header>

      {probe.state === 'checking' && (
        <p className="bb-status" role="status">Loading the bed board</p>
      )}

      {probe.state === 'off' && <ModuleOffNotice subject="adt" state="off" />}

      {probe.state === 'unreachable' && (
        <section className="bb-notice bb-notice--error" role="alert">
          <p className="bb-notice__body">{probe.message}</p>
          <button type="button" className="bb-btn" onClick={retry}>Try again</button>
        </section>
      )}

      {probe.state === 'ready' && shown.length === 0 && (
        <section className="bb-empty" aria-labelledby="bb-none-title">
          <h2 id="bb-none-title" className="bb-empty__title">Nothing here for your role</h2>
          <p className="bb-empty__body">
            None of the Beds screens are open to your role. Ask your administrator if
            you need one.
          </p>
        </section>
      )}

      {probe.state === 'ready' && active && (
        <>
          {shown.length > 1 && (
            <nav className="bb-tabs" aria-label="Beds screens">
              {shown.map((view) => (
                <button
                  key={view.id}
                  type="button"
                  className={`bb-tab${view.id === active.id ? ' bb-tab--on' : ''}`}
                  aria-current={view.id === active.id ? 'page' : undefined}
                  onClick={() => setChosenId(view.id)}
                >
                  {view.label}
                </button>
              ))}
            </nav>
          )}
          <section className="bb-view" aria-label={active.label}>
            <active.Component
              meta={probe.meta}
              openView={openView}
              onModuleOff={onModuleOff}
              holds={holds}
            />
          </section>
        </>
      )}
    </div>
  )
}
