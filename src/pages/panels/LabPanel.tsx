/**
 * Orb, Laboratory: the shell for the laboratory information system (lis-1).
 *
 * AN OPT-IN MODULE. The server switch is ORB_LAB and it is OFF unless a site
 * turns it on; the shell shows this tab only when /api/health says the module
 * is on (or draining) AND this file is in the build. Lab roles whose module is
 * off see the shell's switched-off notice instead of an empty rail.
 *
 * THIS FILE NAMES NO VIEW. Every laboratory screen is its own file in ./lab/,
 * found by `import.meta.glob('./lab/*View.tsx')`; the contract a view file
 * follows is in ./lab/viewRegistry.ts. Each later increment therefore lands
 * as one new file and never edits this one.
 *
 * WHAT IT SHOWS, IN ORDER OF PRECEDENCE
 *   1. While GET /api/lab/meta is in flight: a loading line.
 *   2. 404 module_disabled: the laboratory is switched off at this site, said
 *      in words and never drawn as an empty laboratory. A view that meets the
 *      same answer later calls `onModuleOff` and the panel shows the same
 *      notice.
 *   3. No reply: the server could not be reached, with a way to try again.
 *   4. Draining (switched off with open work, ruling LIS-R21): a plain note
 *      that new work has stopped and open work can still be finished, above
 *      the views that finish work. Views that only start new work are hidden.
 *   5. Otherwise the views this user may see, as tabs. With none installed, an
 *      honest empty state that says the screens have not been added yet; with
 *      some installed but none open to this role, a sentence saying so.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import { getLabMeta, toLabError, type LabMeta } from '../../api/lab'
import ModuleOffNotice from '../../components/ModuleOffNotice'
import { hasPermission, type Permission } from '../../lib/roles'
import { discoverViews, visibleViews, type LabViewEntry } from './lab/viewRegistry'
import './LabPanel.css'

/** Every installed view, read once at module load (the glob is compile-time). */
const INSTALLED_VIEWS: readonly LabViewEntry[] = discoverViews(
  import.meta.glob('./lab/*View.tsx', { eager: true }),
)

/** The client permission mirror. A permission it does not list reads as not
 *  held, so a view gated on it stays hidden rather than shown. */
function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

type Probe =
  | { state: 'checking' }
  | { state: 'ready'; meta: LabMeta; draining: boolean }
  | { state: 'off' }
  | { state: 'unreachable'; message: string }

interface LabPanelProps {
  /** Tests only: the views to offer. The shell mounts the panel without props,
   *  which means every view found in ./lab/. */
  views?: readonly LabViewEntry[]
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
}

export default function LabPanel({ views = INSTALLED_VIEWS, holds = roleHolds }: LabPanelProps) {
  const [probe, setProbe] = useState<Probe>({ state: 'checking' })
  const [attempt, setAttempt] = useState(0)
  const [chosenId, setChosenId] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getLabMeta().then(
      (meta) => {
        if (!live) return
        const safe = meta ?? {}
        setProbe({ state: 'ready', meta: safe, draining: safe.state === 'draining' })
      },
      (err: unknown) => {
        if (!live) return
        const failure = toLabError(err)
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

  const moduleState = probe.state === 'ready' && probe.draining ? 'draining' : 'on'
  const shown = useMemo(() => visibleViews(views, holds, moduleState), [views, holds, moduleState])
  const active = shown.find((view) => view.id === chosenId) ?? shown[0] ?? null

  const openView = useCallback((viewId: string) => {
    if (shown.some((view) => view.id === viewId)) setChosenId(viewId)
  }, [shown])

  const onModuleOff = useCallback(() => setProbe({ state: 'off' }), [])

  return (
    <div className="lab-panel">
      <header className="lab-head">
        <h1 className="lab-title">Laboratory</h1>
        <p className="lab-sub">Specimens, results and reports from the hospital laboratory</p>
      </header>

      {probe.state === 'checking' && (
        <p className="lab-status" role="status">Loading the laboratory</p>
      )}

      {probe.state === 'off' && <ModuleOffNotice subject="lab" state="off" />}

      {probe.state === 'unreachable' && (
        <section className="lab-notice lab-notice--error" role="alert">
          <p className="lab-notice__body">{probe.message}</p>
          <button type="button" className="lab-btn" onClick={retry}>Try again</button>
        </section>
      )}

      {probe.state === 'ready' && probe.draining && (
        <section className="lab-notice lab-notice--draining" aria-labelledby="lab-draining-title">
          <h2 id="lab-draining-title" className="lab-notice__title">
            The laboratory module is switched off on this site
          </h2>
          <p className="lab-notice__body">
            New laboratory work has stopped. Finish, cancel or release the work that is
            still open, and close every critical result. This screen closes once nothing
            is left open.
          </p>
        </section>
      )}

      {probe.state === 'ready' && views.length === 0 && (
        <section className="lab-empty" aria-labelledby="lab-empty-title">
          <h2 id="lab-empty-title" className="lab-empty__title">No laboratory screens yet</h2>
          <p className="lab-empty__body">
            The screens for collecting specimens, entering and authorising results and
            printing reports have not been added to this version of Orb. Until they are,
            laboratory orders and their results stay on the Orders screen.
          </p>
        </section>
      )}

      {probe.state === 'ready' && views.length > 0 && shown.length === 0 && (
        <section className="lab-empty" aria-labelledby="lab-none-title">
          <h2 id="lab-none-title" className="lab-empty__title">Nothing here for your role</h2>
          <p className="lab-empty__body">
            None of the laboratory screens are open to your role
            {probe.draining ? ' while the module is switched off' : ''}. Ask your
            administrator if you need one.
          </p>
        </section>
      )}

      {probe.state === 'ready' && active && (
        <>
          <nav className="lab-tabs" aria-label="Laboratory screens">
            {shown.map((view) => (
              <button
                key={view.id}
                type="button"
                className={`lab-tab${view.id === active.id ? ' lab-tab--on' : ''}`}
                aria-current={view.id === active.id ? 'page' : undefined}
                onClick={() => setChosenId(view.id)}
              >
                {view.label}
              </button>
            ))}
          </nav>
          <section className="lab-view" aria-label={active.label}>
            <active.Component
              meta={probe.meta}
              state={probe.draining ? 'draining' : 'on'}
              openView={openView}
              onModuleOff={onModuleOff}
            />
          </section>
        </>
      )}
    </div>
  )
}
