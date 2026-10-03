/**
 * Orb, Pharmacy store: the shell for the hospital pharmacy's own stock work
 * (purchasing, goods receipt, stock, transfers, expiry, counts, NDPS).
 *
 * AN OPT-IN MODULE. The server switch is ORB_PHARMACY_STORE and it is OFF unless
 * a site turns it on; the shell shows this tab only when /api/health says the
 * module is on AND this file is in the build. Drug stock and dispensing stay in
 * Helix (the Pharmacy screen) either way: this module adds the store around it,
 * it never keeps a second count of the same medicine.
 *
 * THIS FILE NAMES NO VIEW. Every screen of the store is its own file in
 * ./pharmacyStore/, found by `import.meta.glob('./pharmacyStore/*View.tsx')`;
 * the contract a view file follows is in ./pharmacyStore/viewRegistry.ts. Each
 * later area therefore lands as one new file and never edits this one.
 *
 * WHAT IT SHOWS, IN ORDER OF PRECEDENCE
 *   1. While GET /api/pharmacy-store/meta is in flight: a loading line.
 *   2. 404 module_disabled: the module is switched off at this site. That is
 *      said in words, and it is never drawn as an empty store, because an empty
 *      store is a statement about the shelves. A view that meets the same
 *      answer later calls `onModuleOff` and the panel shows the same notice.
 *   3. No reply: the server could not be reached, with a way to try again.
 *   4. Otherwise the views this user may see, as tabs. With none installed, an
 *      honest empty state that says the screens have not been added yet and
 *      where drug stock is kept meanwhile; with some installed but none open to
 *      this role, a sentence saying so.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  getPharmacyStoreMeta,
  toPharmacyStoreError,
  type PharmacyStoreMeta,
} from '../../api/pharmacyStore'
import { hasPermission, type Permission } from '../../lib/roles'
import {
  discoverViews,
  visibleViews,
  type PharmacyStoreViewEntry,
} from './pharmacyStore/viewRegistry'
import './PharmacyStorePanel.css'

/** Every installed view, read once at module load (the glob is compile-time). */
const INSTALLED_VIEWS: readonly PharmacyStoreViewEntry[] = discoverViews(
  import.meta.glob('./pharmacyStore/*View.tsx', { eager: true }),
)

/** The client permission mirror. A permission it does not list reads as not
 *  held, so a view gated on it stays hidden rather than shown. */
function roleHolds(permission: string): boolean {
  return hasPermission(permission as Permission)
}

type Probe =
  | { state: 'checking' }
  | { state: 'ready'; meta: PharmacyStoreMeta }
  | { state: 'off' }
  | { state: 'unreachable'; message: string }

interface PharmacyStorePanelProps {
  /** Tests only: the views to offer. The shell mounts the panel without props,
   *  which means every view found in ./pharmacyStore/. */
  views?: readonly PharmacyStoreViewEntry[]
  /** Tests only: whether the signed-in user holds a permission. */
  holds?: (permission: string) => boolean
}

export default function PharmacyStorePanel({
  views = INSTALLED_VIEWS,
  holds = roleHolds,
}: PharmacyStorePanelProps) {
  const [probe, setProbe] = useState<Probe>({ state: 'checking' })
  const [attempt, setAttempt] = useState(0)
  const [chosenId, setChosenId] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    getPharmacyStoreMeta().then(
      (meta) => {
        if (live) setProbe({ state: 'ready', meta: meta ?? {} })
      },
      (err: unknown) => {
        if (!live) return
        const failure = toPharmacyStoreError(err)
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
    <div className="ps-panel">
      <header className="ps-head">
        <h1 className="ps-title">Pharmacy store</h1>
        <p className="ps-sub">Purchasing, receipts and stock for the hospital pharmacy</p>
      </header>

      {probe.state === 'checking' && (
        <p className="ps-status" role="status">Loading the pharmacy store</p>
      )}

      {probe.state === 'off' && (
        <section className="ps-notice ps-notice--off" aria-labelledby="ps-off-title">
          <h2 id="ps-off-title" className="ps-notice__title">
            The pharmacy store is switched off on this site
          </h2>
          <p className="ps-notice__body">
            Drug stock and dispensing are kept in the Pharmacy screen. Ask your
            administrator if this site should use the pharmacy store.
          </p>
        </section>
      )}

      {probe.state === 'unreachable' && (
        <section className="ps-notice ps-notice--error" role="alert">
          <p className="ps-notice__body">{probe.message}</p>
          <button type="button" className="ps-btn" onClick={retry}>Try again</button>
        </section>
      )}

      {probe.state === 'ready' && views.length === 0 && (
        <section className="ps-empty" aria-labelledby="ps-empty-title">
          <h2 id="ps-empty-title" className="ps-empty__title">No pharmacy store screens yet</h2>
          <p className="ps-empty__body">
            The screens for drug purchasing, goods receipt and stock have not been
            added to this version of Orb. Until they are, drug stock and dispensing
            are kept in the Pharmacy screen.
          </p>
        </section>
      )}

      {probe.state === 'ready' && views.length > 0 && shown.length === 0 && (
        <section className="ps-empty" aria-labelledby="ps-none-title">
          <h2 id="ps-none-title" className="ps-empty__title">Nothing here for your role</h2>
          <p className="ps-empty__body">
            None of the pharmacy store screens are open to your role. Ask your
            administrator if you need one.
          </p>
        </section>
      )}

      {probe.state === 'ready' && active && (
        <>
          <nav className="ps-tabs" aria-label="Pharmacy store screens">
            {shown.map((view) => (
              <button
                key={view.id}
                type="button"
                className={`ps-tab${view.id === active.id ? ' ps-tab--on' : ''}`}
                aria-current={view.id === active.id ? 'page' : undefined}
                onClick={() => setChosenId(view.id)}
              >
                {view.label}
              </button>
            ))}
          </nav>
          <section className="ps-view" aria-label={active.label}>
            <active.Component meta={probe.meta} openView={openView} onModuleOff={onModuleOff} />
          </section>
        </>
      )}
    </div>
  )
}
