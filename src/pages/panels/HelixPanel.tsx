import { useEffect, useState, useCallback, useRef } from 'react'
import type { KeyboardEvent } from 'react'
import styles from '../../components/helix/Helix.module.css'
import HelixDashboard from '../../components/helix/HelixDashboard'
import HelixPatientBoard from '../../components/helix/HelixPatientBoard'
import HelixFormulary from '../../components/helix/HelixFormulary'
import HelixOrderReviewModal from '../../components/helix/HelixOrderReviewModal'
import { apiFetch } from '../../api/client'
import { useVisiblePolling } from '../../hooks/useVisiblePolling'
import { formatUTCTime } from '../../lib/datetime'

type HelixView = 'dashboard' | 'patients' | 'formulary'

const VIEWS: { id: HelixView; label: string }[] = [
  { id: 'dashboard', label: 'Overview' },
  { id: 'patients', label: 'Patient Board' },
  { id: 'formulary', label: 'Formulary' },
]

/* The queue is a live work list. It loaded once per visit, so an order a
   clinician approved after the pharmacist opened the tab never appeared until
   something forced a reload. */
const REFRESH_MS = 30_000

/** initial: the first load, which has nothing to show yet. manual: the Refresh
 *  button or a finished review. poll: the background timer, which never
 *  replaces the lists with a loading state. */
type LoadMode = 'initial' | 'manual' | 'poll'

export default function HelixPanel() {
  const [activeView, setActiveView] = useState<HelixView>('dashboard')
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [orders, setOrders] = useState<any[]>([])
  const [inventory, setInventory] = useState<any[]>([])
  const [catalog, setCatalog] = useState<any[]>([])
  const [selectedOrder, setSelectedOrder] = useState<any | null>(null)
  /* A failed load used to console.error and fall through with orders=[], so the
     pharmacist's work queue rendered EMPTY — indistinguishable from "nothing to
     verify". Orders would sit unverified with the screen saying all clear. */
  const [error, setError] = useState<string | null>(null)
  /* Once lists are on screen, a failed refresh keeps them (blanking a queue the
     pharmacist is reading is worse) but the header says they may be old. */
  const [refreshFailed, setRefreshFailed] = useState(false)
  const [loadedAt, setLoadedAt] = useState<string | null>(null)
  const loadedOnce = useRef(false)
  // Only the newest request may write: a slow poll must not overwrite a newer refresh.
  const loadSeq = useRef(0)

  const loadData = useCallback(async (mode: LoadMode = 'initial') => {
    const seq = ++loadSeq.current
    if (mode === 'initial') setLoading(true)
    if (mode === 'manual') setRefreshing(true)
    try {
      const [ordersRes, invRes, catRes] = await Promise.all([
        apiFetch<any>('/helix/orders'),
        apiFetch<any>('/helix/inventory'),
        apiFetch<any>('/helix/catalog')
      ])
      if (seq !== loadSeq.current) return
      setOrders(ordersRes.orders || [])
      setInventory(invRes.inventory || [])
      setCatalog(catRes.catalog || [])
      setError(null)
      setRefreshFailed(false)
      setLoadedAt(new Date().toISOString())
      loadedOnce.current = true
    } catch (err) {
      console.error("Failed to load Helix data", err)
      if (seq !== loadSeq.current) return
      if (loadedOnce.current) setRefreshFailed(true)
      else setError((err as Error)?.message || 'Could not reach the pharmacy service.')
    } finally {
      if (mode === 'initial') setLoading(false)
      if (mode === 'manual') setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    loadData()
  }, [loadData])

  // Paused while an order is open: the lists re-rendering under a pharmacist
  // mid-review would be hostile, and closing the review refreshes them anyway.
  useVisiblePolling(() => { void loadData('poll') }, REFRESH_MS, !selectedOrder)

  const handleTransitionComplete = () => {
    setSelectedOrder(null)
    loadData('manual')
  }

  /* Arrow keys move between the views, per the tablist pattern: one Tab stop
     for the whole strip, not three. */
  const onTabKey = (e: KeyboardEvent<HTMLButtonElement>) => {
    const i = VIEWS.findIndex(v => v.id === activeView)
    let next = -1
    if (e.key === 'ArrowRight') next = (i + 1) % VIEWS.length
    else if (e.key === 'ArrowLeft') next = (i - 1 + VIEWS.length) % VIEWS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = VIEWS.length - 1
    if (next < 0) return
    e.preventDefault()
    setActiveView(VIEWS[next].id)
    const tabs = e.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')
    tabs?.[next]?.focus()
  }

  return (
    <div className={styles.container}>
      <header className={`orb-home__header ${styles.header}`}>
        <div>
          <h2 className="orb-home__title">Pharmacy Operations</h2>
          <p
            className={`orb-home__context${refreshFailed ? ' orb-home__context--very-stale' : ''}`}
            role={refreshFailed ? 'alert' : undefined}
          >
            Medication orders, verification, and fulfillment
            {loadedAt && <> · updated <span className="orb-home__clock">{formatUTCTime(loadedAt)}</span></>}
            {refreshFailed && <> · the last refresh failed, so these lists may be out of date</>}
          </p>
        </div>
        <div className={styles.headerActions}>
          <div className={styles.tabs} role="tablist" aria-label="Pharmacy views">
            {VIEWS.map(v => (
              <button
                key={v.id}
                type="button"
                role="tab"
                id={`helix-tab-${v.id}`}
                aria-selected={activeView === v.id}
                aria-controls="helix-view"
                tabIndex={activeView === v.id ? 0 : -1}
                className={`${styles.tab} ${activeView === v.id ? styles.activeTab : ''}`}
                onClick={() => setActiveView(v.id)}
                onKeyDown={onTabKey}
              >
                {v.label}
              </button>
            ))}
          </div>
          {/* Never the only path to fresh data: the 30 s poll continues behind it. */}
          <button
            type="button"
            className="orb-home__refresh"
            onClick={() => { void loadData('manual') }}
            disabled={loading || refreshing}
            aria-busy={refreshing || undefined}
          >
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </header>

      <div id="helix-view" role="tabpanel" aria-labelledby={`helix-tab-${activeView}`} className={styles.viewPanel}>
        {loading ? (
          <div className={styles.loading}>Connecting to Helix...</div>
        ) : error ? (
          <div className={styles.loadError} role="alert">
            <strong>Pharmacy queue did not load.</strong>
            <span>
              This is a connection failure, not an empty queue. Orders may be waiting
              that are not shown here. {error}
            </span>
            <button type="button" className={styles.loadErrorRetry} onClick={() => { void loadData() }}>Retry</button>
          </div>
        ) : (
          <>
            {activeView === 'dashboard' && (
              <HelixDashboard orders={orders} onSelectOrder={setSelectedOrder} />
            )}
            {activeView === 'patients' && (
              <HelixPatientBoard orders={orders} inventory={inventory} catalog={catalog} />
            )}
            {activeView === 'formulary' && (
              <HelixFormulary catalog={catalog} inventory={inventory} />
            )}
          </>
        )}
      </div>

      {/* Order Review Modal */}
      {selectedOrder && (
        <HelixOrderReviewModal
          order={selectedOrder}
          catalog={catalog}
          onClose={() => setSelectedOrder(null)}
          onTransition={handleTransitionComplete}
        />
      )}
    </div>
  )
}
