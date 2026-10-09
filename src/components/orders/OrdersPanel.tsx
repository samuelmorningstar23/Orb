import { lazy, Suspense, useCallback, useEffect, useId, useMemo, useState, useSyncExternalStore, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import styles from './Orders.module.css'
import { apiFetch, createOrder, getPatients, isSafetyAckRequired, type MedicationSafety, type Patient, type SafetyAck } from '../../api/client'
import { toLabError } from '../../api/lab'
import { searchLabCatalogue, type LabCatalogueEntry } from '../../api/lab/master'
import { cancelLabOrder, type LabOrderFields, type LabOrderSummary } from '../../api/lab/orders'
import SafetyAlertModal from '../vigil/SafetyAlertModal'
import PatientPicker from '../common/PatientPicker'
import { useDataChanged } from '../../hooks/useDataChanged'
import { formatClinicalDateTime } from '../../lib/datetime'
import { hasPermission, MEDICATION_PRESCRIBE } from '../../lib/roles'
import { currentModuleFlags, subscribeModuleFlags } from '../../shellConfig'
import {
  createCpoeMedicationOrder, errorCode, signMedOrder, type LinkedMedOrder,
  type MedOrderPriority,
} from '../../api/medorders'
import {
  MED_NOT_ON_FORMULARY, MED_NOT_SENT, canBeSigned, isMedicationOrder, linkedOrderLine,
  medicationRoutingNotice, useMedicationLifecycle,
} from './medicationRouting'

// Lazily loaded so the print-only Orders bundle is not pulled into the main
// panel chunk until a clinician actually prints the orders list.
const OrdersPrintView = lazy(() => import('../print/OrdersPrintView'))
// Lazy for the same reason: the protocol library is a second surface under the
// Orders tab and does not belong in the panel's first paint.
const OrderSetsPanel = lazy(() => import('./OrderSetsPanel'))

/** Orders ⇄ Order Sets. A real tablist, so it is keyboard- and SR-navigable. */
function OrdersViewSwitch({
  view, onChange,
}: { view: 'orders' | 'sets'; onChange: (v: 'orders' | 'sets') => void }) {
  const tab = (id: 'orders' | 'sets', label: string) => (
    <button
      type="button"
      role="tab"
      aria-selected={view === id}
      className={view === id ? styles.viewTabOn : styles.viewTab}
      onClick={() => onChange(id)}
    >
      {label}
    </button>
  )
  return (
    <div className={styles.viewTabs} role="tablist" aria-label="Orders view">
      {tab('orders', 'Individual orders')}
      {tab('sets', 'Order sets')}
    </div>
  )
}

/**
 * Orders / CPOE-lite panel — lists a patient's orders with status chips and a
 * create-order form with live terminology code validation.
 *
 * LABORATORY ORDERS (lis-7). An order the laboratory has taken in
 * (lab_managed on the list) is the laboratory's for life: it shows where each
 * of its tests is instead of the next-status and "+ Result" buttons, which the
 * server would refuse (409 order_managed_by_lab), and its Cancel goes through
 * the laboratory's cancel with a required reason. While the laboratory module
 * is on, a new laboratory order is chosen from the laboratory catalogue
 * (never an ICD-10 code), which sets its code and shows the tube to collect.
 *
 * MEDICATION ORDERS THE ORDER STORE MANAGES (medorders-7). With the server's
 * medication-order lifecycle on, a medicine placed here (or by an order set)
 * is a draft a prescriber signs; the signature puts it on the ward chart and
 * the drug round. Such an order (managed_by_medication_order on the list)
 * shows where it is instead of the next-status button, which the server
 * would refuse (409 managed_by_medication_order); a prescriber gets Sign and
 * an "Awaiting my signature" filter, and Cancel needs a reason and is offered
 * only before it is signed. The order form then asks for the medicine as
 * fields (drug, dose, route, how often, as needed). With the lifecycle off
 * every medication line still says it is not sent to the ward or pharmacy.
 */

interface Order extends LabOrderFields {
  id: string
  patient_id: string
  order_type: string
  code: string | null
  code_system: string | null
  description: string
  priority: string
  status: string
  source: string
  result_text: string | null
  result_at: string | null
  created_at: string
  /** medorders-7: the order store owns this medicine's lifecycle. */
  managed_by_medication_order?: boolean
  medication_order?: LinkedMedOrder
}

/** The linked medication order, when the order store manages this order. */
function linkedOf(order: Order): LinkedMedOrder | null {
  return order.managed_by_medication_order === true && order.medication_order
    ? order.medication_order : null
}

/** Unsigned linked drafts, oldest first (ruling MO-R10 (a): the oldest wait
 *  longest and come first). */
function awaitingSignature(orders: Order[]): Order[] {
  return orders
    .filter(o => canBeSigned(linkedOf(o)))
    .sort((a, b) => (linkedOf(a)?.created_at ?? a.created_at)
      .localeCompare(linkedOf(b)?.created_at ?? b.created_at))
}

/** Words for a refused signature, in the server's own sentence when it sent
 *  one. */
function signFailure(err: unknown): string {
  const code = errorCode(err)
  if (code === 'stale_version' || code === 'screen_stale' || code === 'cpoe_order_changed') {
    return `${(err as Error).message || 'This order changed.'} The list has been refreshed.`
  }
  return (err as Error)?.message || 'the order could not be signed.'
}

interface Props {
  patientId?: string
}

const ORDER_TYPES = ['lab', 'imaging', 'medication', 'procedure', 'nursing', 'other']
const CODE_SYSTEMS = ['LOINC', 'SNOMED', 'ICD10']
const PRIORITIES = ['routine', 'urgent', 'stat']
const CODELESS = new Set(['nursing', 'other'])
const RESULTABLE = new Set(['lab', 'imaging'])

const CHIP_CLASS: Record<string, string> = {
  draft: styles.chipDraft,
  active: styles.chipActive,
  in_progress: styles.chipInProgress,
  completed: styles.chipCompleted,
  resulted: styles.chipResulted,
  cancelled: styles.chipCancelled,
}

// `cancelled` is terminal in the CPOE state machine and there is no un-cancel
// endpoint, so the Cancel control is visually separated from the adjacent
// forward-transition button and never fires on a single click.
const CANCEL_BTN_STYLE: CSSProperties = { borderColor: 'color-mix(in srgb, var(--status-danger) 28%, transparent)' }
const CONFIRM_CANCEL_BTN_STYLE: CSSProperties = {
  background: 'var(--status-danger)',
  borderColor: 'var(--status-danger)',
  color: 'var(--status-on-fill)',
  fontWeight: 600,
}

/** Next legal forward status in the CPOE state machine. */
function nextStatus(order: Order): string | null {
  switch (order.status) {
    case 'draft': return 'active'
    case 'active': return 'in_progress'
    case 'in_progress': return 'completed'
    default: return null
  }
}

const LAB_TEST_LIST_STYLE: CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: '2px 12px',
  margin: '4px 0 0',
  padding: 0,
  listStyle: 'none',
}

/** Where each test of a laboratory-managed order is, in the ward's words
 *  (lis-7). The values themselves are on the laboratory report. */
function LabOrderStatus({ summary }: { summary: LabOrderSummary }) {
  const cancelled = summary.cancelled
  return (
    <div className={styles.result} role="group" aria-label="Laboratory status">
      <div>Laboratory: {summary.summary}</div>
      <ul style={LAB_TEST_LIST_STYLE}>
        {summary.tests.map(test => (
          <li key={test.test_id}>{test.short_name || test.name}: {test.status_text}</li>
        ))}
      </ul>
      {summary.rejected > 0 && (
        <div>
          The laboratory rejected {summary.rejected === 1 ? 'a sample' : `${summary.rejected} samples`}:
          {' '}it waits for a recollection or a cancel.
        </div>
      )}
      {cancelled && (
        <div>
          Cancelled{cancelled.by_name ? ` by ${cancelled.by_name}` : ''}
          {cancelled.at ? ` at ${formatClinicalDateTime(cancelled.at)}` : ''}
          {cancelled.reason ? `: ${cancelled.reason}` : ''}
        </div>
      )}
    </div>
  )
}

/**
 * The laboratory catalogue as a typeahead (lis-7), shown for a laboratory
 * order while the module is on. Choosing a test sets its code and code system
 * (LOINC or the laboratory's own), so an ICD-10 code can never be sent as a
 * laboratory test, and shows the tube to collect. A bedside test says so: it
 * stays on the ward and keeps the free-text result.
 */
function LabTestPicker({
  selected, onSelect,
}: { selected: LabCatalogueEntry | null; onSelect: (entry: LabCatalogueEntry | null) => void }) {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<LabCatalogueEntry[]>([])
  const [searching, setSearching] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const fid = useId()

  useEffect(() => {
    const term = query.trim()
    if (selected || term.length < 2) return undefined
    let live = true
    const timer = setTimeout(() => {
      setSearching(true)
      searchLabCatalogue(term, 10).then(
        res => { if (live) { setResults(res.tests ?? []); setError(null) } },
        err => { if (live) { setResults([]); setError(toLabError(err).message) } },
      ).finally(() => { if (live) setSearching(false) })
    }, 250)
    return () => { live = false; clearTimeout(timer) }
  }, [query, selected])

  if (selected) {
    const tube = [selected.container, selected.specimen_display].filter(Boolean).join(', ')
    return (
      <div className={styles.field}>
        <span className={styles.label}>Laboratory test</span>
        <div className={styles.validateRow}>
          <span className={styles.valid}>
            {selected.name} ({selected.code_system} {selected.code})
          </span>
          <button type="button" className={styles.actionBtn} onClick={() => { onSelect(null); setQuery('') }}>
            Change
          </button>
        </div>
        <span className={styles.validateHint}>
          {selected.performed_at === 'ward_poct'
            ? 'Done on the ward: it does not go to the laboratory.'
            : `Collect in: ${tube || 'tube not set in the catalogue'}`}
          {selected.kind === 'panel' && selected.components?.length
            ? `. Includes ${selected.components.map(c => c.short_name || c.name).filter(Boolean).join(', ')}.`
            : ''}
        </span>
      </div>
    )
  }

  const shown = query.trim().length >= 2 ? results : []
  return (
    <div className={styles.field}>
      <label className={styles.label} htmlFor={`${fid}-lab`}>Laboratory test</label>
      <input
        id={`${fid}-lab`}
        className={styles.input}
        value={query}
        onChange={e => setQuery(e.target.value)}
        placeholder="Search the laboratory catalogue, e.g. potassium"
        autoComplete="off"
        role="combobox"
        aria-expanded={shown.length > 0}
        aria-controls={`${fid}-lab-list`}
      />
      {searching && <span className={styles.validateHint}>Searching…</span>}
      {error && <span className={styles.invalid}>{error}</span>}
      {!searching && !error && query.trim().length >= 2 && shown.length === 0 && (
        <span className={styles.validateHint}>No test in the laboratory catalogue matches.</span>
      )}
      {shown.length > 0 && (
        <ul id={`${fid}-lab-list`} role="listbox" aria-label="Laboratory tests" style={{ listStyle: 'none', margin: '4px 0 0', padding: 0 }}>
          {shown.map(entry => (
            <li key={entry.id} role="option" aria-selected={false}>
              <button type="button" className={styles.actionBtn} style={{ width: '100%', textAlign: 'left' }}
                onClick={() => onSelect(entry)}>
                {entry.name} · {entry.code_system} {entry.code}
                {entry.performed_at === 'ward_poct'
                  ? ' · done on the ward'
                  : entry.container ? ` · ${entry.container}` : ''}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export default function OrdersPanel({ patientId: patientIdProp }: Props) {
  /* Two surfaces live under Orders: individual order entry, and the protocol
     (order set) library. The library had a complete, safety-enforced backend
     and no UI at all, so a listed demo step could not be performed. It is a
     sub-tab rather than a new sidebar entry because it is the same job — put
     orders on a patient — and because the sidebar is already eleven items. */
  const [view, setView] = useState<'orders' | 'sets'>('orders')
  const [patientId, setPatientId] = useState(patientIdProp || '')
  const [orders, setOrders] = useState<Order[]>([])
  const [loading, setLoading] = useState(false)
  // Last load failure. Without it a failed fetch renders "No orders for this
  // patient yet." — an affirmative claim the code cannot back, and one a
  // clinician can act on by re-entering orders that already exist.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [printing, setPrinting] = useState(false)
  // Order id whose cancellation is awaiting confirmation, plus the optional
  // reason recorded on the order_events trail with the transition.
  const [cancelTarget, setCancelTarget] = useState<string | null>(null)
  const [cancelReason, setCancelReason] = useState('')
  // Result entry happens in the order's own card. It used to be a browser
  // prompt() that named no patient and lost the text on any slip.
  const [resultTarget, setResultTarget] = useState<string | null>(null)
  const [resultText, setResultText] = useState('')
  const [resultBusy, setResultBusy] = useState(false)
  // Why the last status change or result failed, shown above the list. A
  // browser alert() here blocked the screen and the message was gone once
  // dismissed.
  const [actionError, setActionError] = useState<string | null>(null)
  // The same scoped list the picker shows, so the order form can name the
  // patient it is writing for. A failed load only costs the name, never the form.
  const [patients, setPatients] = useState<Patient[]>([])
  // medorders-7: the prescriber's Sign, and the "Awaiting my signature" view.
  const lifecycleOn = useMedicationLifecycle()
  // Read only with the lifecycle on: with it off nothing here can be signed.
  const canPrescribe = lifecycleOn && hasPermission(MEDICATION_PRESCRIBE)
  const [signView, setSignView] = useState(false)
  const [signBusy, setSignBusy] = useState<string | null>(null)
  const [signModal, setSignModal] = useState<{ order: Order; safety: MedicationSafety } | null>(null)
  // A same-medicine notice from the signature (a notice only until ruling MO-R6).
  const [signNotice, setSignNotice] = useState<string | null>(null)

  /* Follow the patient the shell hands us, and drop anything still referring to
     the patient we just left. Both adjustments happen DURING RENDER (React's
     documented "adjusting state when a prop changes") rather than in effects.
     That is not a style preference here: an effect runs AFTER the commit, so
     the render that received the new patientIdProp still painted the previous
     patient's id — and `loadOrders` is keyed on it — which put one frame of the
     previous patient's order list under the new patient's header. Adjusting in
     render makes React re-run this component before anything reaches the
     screen, so that frame does not exist. */
  const [syncedProp, setSyncedProp] = useState(patientIdProp)
  if (patientIdProp !== syncedProp) {
    setSyncedProp(patientIdProp)
    if (patientIdProp) setPatientId(patientIdProp)
  }

  // A pending cancel confirmation refers to an order in the previous patient's
  // list, so it must not survive a patient switch.
  const [cancelPatient, setCancelPatient] = useState(patientId)
  if (patientId !== cancelPatient) {
    setCancelPatient(patientId)
    setCancelTarget(null)
    setResultTarget(null)
    setActionError(null)
    setSignModal(null)
    setSignNotice(null)
  }

  const loadOrders = useCallback(async () => {
    if (!patientId) { setOrders([]); setLoadError(null); return }
    setLoading(true)
    try {
      const res = await apiFetch<{ orders: Order[] }>(`/orders/patient/${patientId}`)
      setOrders(res.orders || [])
      setLoadError(null)
    } catch (err) {
      console.error('Failed to load orders', err)
      setLoadError(err instanceof Error ? err.message : 'Orders could not be loaded.')
    } finally {
      setLoading(false)
    }
  }, [patientId])

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { loadOrders() }, [loadOrders])

  // Bumped when a patient is admitted elsewhere in the shell, so the order
  // form can name a patient admitted after this panel first loaded.
  const [patientsRev, setPatientsRev] = useState(0)
  useDataChanged(() => setPatientsRev(n => n + 1))

  useEffect(() => {
    let live = true
    getPatients()
      .then(list => { if (live) setPatients(Array.isArray(list) ? list : []) })
      .catch(() => { /* the form falls back to an unnamed header */ })
    return () => { live = false }
  }, [patientsRev])

  const selectedPatient = patients.find(p => p.id === patientId) || null

  /* The list is only loaded on a patient change, and the Order sets view lives
     under the same component, so coming back from applying a protocol showed
     the list as it was before the set's orders existed. Reload on return, and
     on apply (below) for good measure. */
  const switchView = (v: 'orders' | 'sets') => {
    setView(v)
    if (v === 'orders') void loadOrders()
  }

  const transition = async (order: Order, to: string, detail?: string) => {
    setActionError(null)
    try {
      await apiFetch(`/orders/${order.id}/transition`, {
        method: 'POST',
        body: JSON.stringify(detail ? { to_status: to, detail } : { to_status: to }),
      })
      loadOrders()
    } catch (err) {
      console.error('Transition failed', err)
      setActionError(`${order.description}: ${(err as Error).message || 'the order could not be updated.'}`)
    }
  }

  const askCancel = (order: Order) => {
    setCancelReason('')
    setCancelTarget(order.id)
  }

  const confirmCancel = (order: Order) => {
    const reason = cancelReason.trim()
    if (linkedOf(order)) {
      // The order store cancels an unsigned medicine, and needs a reason: it
      // stays on the medicine's own record.
      if (!reason) return
      setCancelTarget(null)
      setCancelReason('')
      transition(order, 'cancelled', reason)
      return
    }
    if (order.lab_managed) {
      // The laboratory's cancel needs a reason: the bench reads it.
      if (!reason) return
      setCancelTarget(null)
      setCancelReason('')
      void cancelThroughLab(order, reason)
      return
    }
    setCancelTarget(null)
    setCancelReason('')
    transition(order, 'cancelled', reason || undefined)
  }

  /* lis-7: an order the laboratory manages is cancelled through the
     laboratory, which cancels every test not yet reported and keeps the
     reported ones. The Orders screen's own cancel would answer 409. */
  const cancelThroughLab = async (order: Order, reason: string) => {
    setActionError(null)
    try {
      await cancelLabOrder(order.id, reason)
      loadOrders()
    } catch (err) {
      const failure = toLabError(err)
      setActionError(`${order.description}: ${failure.isModuleDisabled
        ? 'the laboratory module is switched off on this site, so the laboratory cannot cancel it. Call the laboratory.'
        : failure.message || 'the order could not be cancelled.'}`)
    }
  }

  /* medorders-7: the prescriber's signature. The server screens the medicine
     again with the signer's role (428 for an alert to acknowledge) and puts
     it on the ward chart and the drug round. */
  const sign = async (order: Order, safetyAck?: SafetyAck) => {
    const linked = linkedOf(order)
    if (!linked) return
    setActionError(null)
    setSignNotice(null)
    setSignBusy(order.id)
    try {
      const res = await signMedOrder(linked.id, linked.version, safetyAck)
      setSignModal(null)
      const same = (res.duplicates ?? []).map(d => d.drug_name).filter(Boolean)
      if (same.length) {
        setSignNotice(`${order.description}: signed. Already on this patient's chart: ` +
          `${same.join(', ')}. Check it is not given twice.`)
      }
      loadOrders()
    } catch (err) {
      if (isSafetyAckRequired(err)) {
        setSignModal({ order, safety: err.safety })
        return
      }
      setSignModal(null)
      setActionError(`${order.description}: ${signFailure(err)}`)
      loadOrders()
    } finally {
      setSignBusy(null)
    }
  }

  const openResult = (order: Order) => {
    setActionError(null)
    setResultText('')
    setResultTarget(order.id)
  }

  const saveResult = async (order: Order) => {
    const text = resultText.trim()
    if (!text) return
    setResultBusy(true)
    setActionError(null)
    try {
      await apiFetch(`/orders/${order.id}/result`, {
        method: 'POST',
        body: JSON.stringify({ result_text: text }),
      })
      setResultTarget(null)
      setResultText('')
      loadOrders()
    } catch (err) {
      // The typed result stays in the box so it can be saved again.
      setActionError(`${order.description}: ${(err as Error).message || 'the result could not be saved.'}`)
    } finally {
      setResultBusy(false)
    }
  }

  if (view === 'sets') {
    return (
      <div className={styles.container}>
        <OrdersViewSwitch view={view} onChange={switchView} />
        <Suspense fallback={<div className={styles.loading}>Loading protocols…</div>}>
          {/* Fixed only when a chart hosts this panel. From the Orders tab the
              selection is SHARED: the protocol screen keeps its picker and
              names the patient, and a patient picked there is the one the
              order list shows on return. */}
          {patientIdProp ? (
            <OrderSetsPanel patientId={patientIdProp} onApplied={loadOrders} />
          ) : (
            <OrderSetsPanel
              selectedPatientId={patientId}
              onSelectPatient={setPatientId}
              onApplied={loadOrders}
            />
          )}
        </Suspense>
      </div>
    )
  }

  return (
    <div className={styles.container}>
      <OrdersViewSwitch view={view} onChange={switchView} />
      <header className={styles.header}>
        <div className={styles.headerLeft}>
          <h2 className={styles.title}>Orders</h2>
          <p className={styles.subtitle}>Place and track orders. Codes are checked before an order is saved.</p>
        </div>
        <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
          {orders.length > 0 && (
            /* Secondary: the header's one filled button is New order. */
            <button
              type="button"
              className={styles.cancelBtn}
              onClick={() => setPrinting(true)}
              disabled={printing}
            >
              {printing ? 'Preparing…' : 'Print'}
            </button>
          )}
          <button
            type="button"
            className={showForm ? styles.cancelBtn : styles.newButton}
            onClick={() => setShowForm(v => !v)}
          >
            {showForm ? 'Close' : '+ New Order'}
          </button>
        </div>
      </header>

      {/* A PICKER, not a free-text id field. The old control was labelled
          "Patient ID" with the placeholder `e.g. PAT001`; typing exactly that
          404'd, because real ids look like PAT-A73E5E98. See PatientPicker. */}
      {!patientIdProp && (
        <div className={styles.formRow}>
          <PatientPicker
            value={patientId}
            onChange={setPatientId}
            label="Patient"
            hint="Pick the patient whose orders you want to see."
          />
        </div>
      )}

      {/* Keyed on the patient: a half-written order must not survive a switch
          to another patient's chart with the form still open. */}
      {showForm && patientId && (
        <CreateOrderForm
          key={patientId}
          patientId={patientId}
          patient={selectedPatient}
          onCreated={() => { setShowForm(false); loadOrders() }}
          onCancel={() => setShowForm(false)}
        />
      )}

      {/* A refresh that failed on top of an already-loaded list keeps the list
          visible, but must not let it read as current. */}
      {loadError && orders.length > 0 && (
        <div className={styles.errorText} role="status" style={{ marginBottom: 8 }}>
          Could not refresh orders, showing the last successful load.{' '}
          <button
            type="button"
            onClick={loadOrders}
            style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
          >
            Retry
          </button>
        </div>
      )}

      {actionError && (
        <div className={styles.errorText} role="alert" style={{ marginBottom: 8 }}>
          {actionError}{' '}
          <button
            type="button"
            onClick={() => setActionError(null)}
            style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
          >
            Dismiss
          </button>
        </div>
      )}

      {signNotice && (
        <div className={styles.medNotSent} role="status" style={{ marginBottom: 8 }}>
          {signNotice}{' '}
          <button
            type="button"
            onClick={() => setSignNotice(null)}
            style={{ background: 'none', border: 'none', padding: 0, color: 'inherit', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
          >
            Dismiss
          </button>
        </div>
      )}

      {lifecycleOn && canPrescribe && patientId && !loading && orders.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
          <button
            type="button"
            className={styles.actionBtn}
            aria-pressed={signView}
            onClick={() => setSignView(v => !v)}
          >
            {signView ? 'Show all orders' : `Awaiting my signature (${awaitingSignature(orders).length})`}
          </button>
          {signView && (
            <span style={{ fontSize: '0.8rem' }}>
              Unsigned medicines for this patient, oldest first.
            </span>
          )}
        </div>
      )}

      {loading ? (
        <div className={styles.loading}>Loading orders…</div>
      ) : loadError && orders.length === 0 ? (
        // The affirmative "no orders" copy is only truthful after a successful
        // fetch; a failed one says so instead, so nobody re-enters live orders.
        <div className={styles.errorState} role="alert">
          <div className={styles.emptyTitle}>Orders could not be loaded</div>
          <div className={styles.emptyDetail}>
            This is a display failure, not an empty chart. Do not read it as “no orders”.
            Check the chart directly if you cannot reload.
          </div>
          <button type="button" className={styles.retryBtn} onClick={loadOrders}>
            Retry
          </button>
        </div>
      ) : orders.length === 0 ? (
        <div className={styles.emptyState}>
          <span className={styles.emptyIcon} aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                 strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2" />
              <rect x="9" y="3" width="6" height="4" rx="1" />
              <path d="M9 13h6" /><path d="M9 17h4" />
            </svg>
          </span>
          <div className={styles.emptyTitle}>
            {patientId ? 'No orders yet' : 'No patient selected'}
          </div>
          <div className={styles.emptyDetail}>
            {patientId
              ? 'Nothing has been ordered for this patient. Use “New Order” to place the first one. It is checked against the code list and the safety checks before it is saved.'
              : 'Search above for a patient and their lab, imaging, medication and procedure orders will appear here, newest first.'}
          </div>
        </div>
      ) : signView && awaitingSignature(orders).length === 0 ? (
        <div className={styles.emptyState}>
          <div className={styles.emptyTitle}>Nothing is waiting for a signature</div>
          <div className={styles.emptyDetail}>
            Every medicine ordered for this patient here has been signed or cancelled.
          </div>
        </div>
      ) : (
        <div className={styles.list}>
          {(signView ? awaitingSignature(orders) : orders).map(order => {
            // lis-7: the laboratory moves and reports its own orders.
            const labManaged = order.lab_managed === true
            // medorders-7: the order store moves its own medicines.
            const linked = linkedOf(order)
            const next = labManaged || linked ? null : nextStatus(order)
            const canResult = !labManaged && RESULTABLE.has(order.order_type) &&
              (order.status === 'active' || order.status === 'in_progress')
            const isTerminal = ['completed', 'resulted', 'cancelled'].includes(order.status)
            // A signed medicine is stopped by a prescriber's own step, not
            // cancelled here (the server refuses it).
            const canCancel = !isTerminal && (!linked || linked.status === 'draft')
            const reasonRequired = labManaged || !!linked
            const showSign = !!linked && canBeSigned(linked) && lifecycleOn && canPrescribe
            return (
              <div key={order.id} className={styles.card}>
                <div className={styles.cardMain}>
                  <div className={styles.cardTitle}>{order.description}</div>
                  <div className={styles.cardMeta}>
                    <span>{order.order_type}</span>
                    {order.code && (
                      <span className={styles.code}>{order.code_system} {order.code}</span>
                    )}
                    <span className={
                      order.priority === 'stat' ? styles.priorityStat
                        : order.priority === 'urgent' ? styles.priorityUrgent : ''
                    }>{order.priority}</span>
                    {order.source !== 'clinician' && <span>{order.source}</span>}
                  </div>
                  {isMedicationOrder(order.order_type) && !linked && (
                    <div className={styles.medNotSent}>{MED_NOT_SENT}</div>
                  )}
                  {linked && (
                    <div
                      className={linked.status === 'draft' ? styles.medNotSent : styles.result}
                      role="note"
                    >
                      {[linked.dose, linked.route, linked.frequency].filter(Boolean).join(' ')}
                      {linked.is_prn ? `, as needed${linked.prn_indication ? ` for ${linked.prn_indication}` : ''}` : ''}
                      {'. '}
                      {linkedOrderLine(linked)}
                      {linked.status === 'draft' && !lifecycleOn && ' Signing is switched off on this computer.'}
                    </div>
                  )}
                  {linked && linked.on_formulary === false && (
                    <div className={styles.medNotSent}>{MED_NOT_ON_FORMULARY}</div>
                  )}
                  {order.result_text && (
                    <div className={styles.result}>Result: {order.result_text}</div>
                  )}
                  {labManaged && order.lab && <LabOrderStatus summary={order.lab} />}
                  {!labManaged && order.lab_intake && (
                    <div className={styles.medNotSent} role="note">{order.lab_intake.message}</div>
                  )}
                  {resultTarget === order.id && (
                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
                      <input
                        className={styles.input}
                        value={resultText}
                        onChange={e => setResultText(e.target.value)}
                        onKeyDown={e => {
                          if (e.key === 'Escape') setResultTarget(null)
                          if (e.key === 'Enter') void saveResult(order)
                        }}
                        placeholder="Result"
                        aria-label={`Result for ${order.description}`}
                        autoFocus
                        style={{ flex: 1, minWidth: 180, padding: '6px 10px', fontSize: '0.8rem' }}
                      />
                      <button
                        type="button"
                        className={styles.actionBtn}
                        disabled={resultBusy || !resultText.trim()}
                        onClick={() => void saveResult(order)}
                      >
                        {resultBusy ? 'Saving…' : 'Save result'}
                      </button>
                      <button type="button" className={styles.actionBtn} onClick={() => setResultTarget(null)}>
                        Cancel
                      </button>
                    </div>
                  )}
                </div>
                <div className={styles.cardRight}>
                  <span className={`${styles.chip} ${CHIP_CLASS[order.status] || ''}`}>
                    {order.status.replace('_', ' ')}
                  </span>
                  {next && (
                    <button className={styles.actionBtn} onClick={() => transition(order, next)}>
                      → {next.replace('_', ' ')}
                    </button>
                  )}
                  {showSign && (
                    <button
                      type="button"
                      className={styles.actionBtn}
                      disabled={signBusy === order.id}
                      onClick={() => void sign(order)}
                      aria-label={`Sign ${order.description}`}
                    >
                      {signBusy === order.id ? 'Signing…' : 'Sign'}
                    </button>
                  )}
                  {canResult && resultTarget !== order.id && (
                    <button className={styles.actionBtn} onClick={() => openResult(order)}>
                      + Result
                    </button>
                  )}
                  {canCancel && (
                    cancelTarget === order.id ? (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                        <span style={{ fontSize: '0.8rem' }}>Cancel order?</span>
                        <input
                          className={styles.input}
                          value={cancelReason}
                          onChange={e => setCancelReason(e.target.value)}
                          onKeyDown={e => {
                            if (e.key === 'Escape') setCancelTarget(null)
                            if (e.key === 'Enter') confirmCancel(order)
                          }}
                          placeholder={reasonRequired ? 'Reason (required)' : 'Reason (optional)'}
                          aria-label={`Cancellation reason for ${order.description}`}
                          autoFocus
                          style={{ width: 150, padding: '6px 10px', fontSize: '0.8rem' }}
                        />
                        <button
                          className={styles.actionBtn}
                          style={reasonRequired && !cancelReason.trim() ? undefined : CONFIRM_CANCEL_BTN_STYLE}
                          disabled={reasonRequired && !cancelReason.trim()}
                          onClick={() => confirmCancel(order)}
                        >
                          Confirm cancel
                        </button>
                        <button className={styles.actionBtn} onClick={() => setCancelTarget(null)}>
                          Keep
                        </button>
                      </div>
                    ) : (
                      <button
                        className={styles.actionBtn}
                        style={CANCEL_BTN_STYLE}
                        onClick={() => askCancel(order)}
                      >
                        Cancel
                      </button>
                    )
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}

      {signModal && (
        <SafetyAlertModal
          safety={signModal.safety}
          mode="blocking"
          drugName={linkedOf(signModal.order)?.drug_name ?? signModal.order.description}
          busy={signBusy === signModal.order.id}
          onAcknowledge={(reason) => void sign(signModal.order, { acknowledged: true, reason })}
          onClose={() => setSignModal(null)}
        />
      )}

      {printing && createPortal(
        <Suspense fallback={null}>
          <OrdersPrintView
            patientId={patientId}
            orders={orders.map(o => ({
              order_type: o.order_type,
              description: o.description,
              priority: o.priority,
              status: o.status,
              result_text: o.result_text,
              created_at: o.created_at,
            }))}
            onAfterPrint={() => setPrinting(false)}
          />
        </Suspense>,
        document.body
      )}
    </div>
  )
}

// ─── Create Order Form ───
interface FormProps {
  patientId: string
  /** null when the patient list has not loaded; the header then says so. */
  patient: Patient | null
  onCreated: () => void
  onCancel: () => void
}

type ValidState = 'idle' | 'checking' | 'valid' | 'invalid'

function CreateOrderForm({ patientId, patient, onCreated, onCancel }: FormProps) {
  const [orderType, setOrderType] = useState('lab')
  const [description, setDescription] = useState('')
  const [codeSystem, setCodeSystem] = useState('LOINC')
  const [code, setCode] = useState('')
  const [priority, setPriority] = useState('routine')
  const [valid, setValid] = useState<ValidState>('idle')
  const [display, setDisplay] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  // Drug-safety gate UI for medication orders: blocking = 428 needs an
  // acknowledged override before the order is created; info = the order was
  // created but the screen raised sub-severe alerts worth a look.
  // `orderId` is the order the info review is about: it already exists, as a
  // draft, so the review offers to cancel it.
  const [safetyModal, setSafetyModal] = useState<{ safety: MedicationSafety; mode: 'blocking' | 'info'; orderId?: string } | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  // lis-7: with the laboratory module on, a laboratory order is chosen from
  // the laboratory catalogue instead of a free code (module off by default:
  // the form is then exactly what it was).
  const modules = useSyncExternalStore(subscribeModuleFlags, currentModuleFlags)
  const labCatalogue = orderType === 'lab' && modules.lab === true
  const [labTest, setLabTest] = useState<LabCatalogueEntry | null>(null)
  // medorders-7: with the medication-order lifecycle on, a medicine is its
  // fields (ruling MO-R11 (a): free text, no code), and the description is
  // written from them so the list and the ward name the same drug.
  const lifecycleOn = useMedicationLifecycle()
  const medicineFields = lifecycleOn && isMedicationOrder(orderType)
  const [drug, setDrug] = useState('')
  const [dose, setDose] = useState('')
  const [route, setRoute] = useState('')
  const [frequency, setFrequency] = useState('')
  const [asNeeded, setAsNeeded] = useState(false)
  const [asNeededFor, setAsNeededFor] = useState('')
  const medicineDescription = [drug, dose, route, frequency].map(v => v.trim()).filter(Boolean).join(' ')
    + (asNeeded ? `, as needed${asNeededFor.trim() ? ` for ${asNeededFor.trim()}` : ''}` : '')

  const needsCode = !CODELESS.has(orderType) && !medicineFields
  // Each label names its control, so a screen reader announces "Priority"
  // on the select rather than an unnamed combo box.
  const fid = useId()

  // The test name the picker last wrote into the description. Choosing a test
  // always rewrites the description to that test, so an order can never be
  // titled with one test while the laboratory receives the code of another.
  // Change (or leaving the laboratory order type) clears the description
  // again only while it is still exactly that name, so the clinician's own
  // words are never wiped by a click that does not choose a test.
  const [autoDescription, setAutoDescription] = useState<string | null>(null)

  const chooseLabTest = (entry: LabCatalogueEntry | null) => {
    setLabTest(entry)
    if (entry) {
      setDescription(entry.name)
      setAutoDescription(entry.name)
      return
    }
    if (autoDescription !== null && description === autoDescription) setDescription('')
    setAutoDescription(null)
  }

  const validateCode = async () => {
    if (!code.trim()) return
    setValid('checking')
    try {
      const res = await apiFetch<{ valid: boolean; display: string | null }>(
        `/orders/terminology/validate?code_system=${encodeURIComponent(codeSystem)}&code=${encodeURIComponent(code)}`
      )
      setValid(res.valid ? 'valid' : 'invalid')
      setDisplay(res.display)
      if (res.valid && res.display && !description.trim()) setDescription(res.display)
    } catch {
      setValid('invalid')
    }
  }

  const canSubmit = useMemo(() => {
    if (medicineFields) {
      return [drug, dose, route, frequency].every(v => v.trim().length > 0)
    }
    if (!description.trim()) return false
    if (labCatalogue) return labTest !== null
    if (needsCode) return valid === 'valid'
    return true
  }, [description, needsCode, valid, labCatalogue, labTest, medicineFields, drug, dose, route, frequency])

  const submit = async (safetyAck?: SafetyAck) => {
    setError(null)
    setSubmitting(true)
    try {
      const res = medicineFields
        ? await createCpoeMedicationOrder({
          patient_id: patientId,
          description: medicineDescription,
          priority: priority as MedOrderPriority,
          drug_name: drug.trim(),
          dose: dose.trim(),
          route: route.trim(),
          frequency: frequency.trim(),
          is_prn: asNeeded,
          prn_indication: asNeeded ? asNeededFor.trim() || null : null,
        }, safetyAck)
        : await createOrder({
          patient_id: patientId,
          order_type: orderType,
          description,
          code: labCatalogue ? labTest?.code ?? null : needsCode && code.trim() ? code.trim() : null,
          code_system: labCatalogue ? labTest?.code_system ?? null
            : needsCode && code.trim() ? codeSystem : null,
          priority,
        }, safetyAck)
      // Sub-severe alerts (or an unscreened drug): the order was created, but
      // show the screen result once before closing the form. Skipped on an
      // acknowledged re-submit — the clinician just reviewed those alerts.
      const safety = res.safety
      if (!safetyAck && safety && (safety.max_severity !== 'none' || safety.interaction_screened === false)) {
        const createdId = res.order?.id
        setCancelError(null)
        setSafetyModal({ safety, mode: 'info', orderId: createdId != null ? String(createdId) : undefined })
      } else {
        setSafetyModal(null)
        onCreated()
      }
    } catch (err) {
      if (isSafetyAckRequired(err)) {
        // 428: severe alerts — open the blocking acknowledgement modal.
        setSafetyModal({ safety: err.safety, mode: 'blocking' })
      } else {
        setError((err as Error).message || 'Failed to create order')
      }
    } finally {
      setSubmitting(false)
    }
  }

  // Undo the draft the review is about. Through the same transition the order
  // list uses, so the cancellation is on the order's own event trail.
  const cancelPlacedOrder = async (orderId: string) => {
    setCancelling(true)
    setCancelError(null)
    try {
      await apiFetch(`/orders/${orderId}/transition`, {
        method: 'POST',
        body: JSON.stringify({ to_status: 'cancelled', detail: 'Cancelled from the drug-safety review' }),
      })
      setSafetyModal(null)
      onCreated()
    } catch (err) {
      setCancelError((err as Error).message || 'The order could not be cancelled')
    } finally {
      setCancelling(false)
    }
  }

  const where = patient ? [patient.bed ? `bed ${patient.bed}` : null, patient.ward].filter(Boolean).join(', ') : ''

  return (
    <div className={styles.formCard}>
      <div className={styles.orderingFor}>
        {patient
          ? <>Ordering for <strong>{patient.name}</strong>{where ? `, ${where}` : ''}</>
          : 'Ordering for the patient selected above'}
      </div>
      <div className={styles.formRow}>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fid}-type`}>Order Type</label>
          <select
            id={`${fid}-type`}
            className={styles.select}
            value={orderType}
            onChange={e => { setOrderType(e.target.value); setValid('idle'); chooseLabTest(null) }}
          >
            {ORDER_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        </div>
        <div className={styles.field}>
          <label className={styles.label} htmlFor={`${fid}-priority`}>Priority</label>
          <select id={`${fid}-priority`} className={styles.select} value={priority} onChange={e => setPriority(e.target.value)}>
            {PRIORITIES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
      </div>

      {labCatalogue && (
        <div className={styles.formRow}>
          <LabTestPicker selected={labTest} onSelect={chooseLabTest} />
        </div>
      )}

      {needsCode && !labCatalogue && (
        <div className={styles.formRow}>
          <div className={styles.field} style={{ maxWidth: 140 }}>
            <label className={styles.label} htmlFor={`${fid}-system`}>Code System</label>
            <select
              id={`${fid}-system`}
              className={styles.select}
              value={codeSystem}
              onChange={e => { setCodeSystem(e.target.value); setValid('idle') }}
            >
              {CODE_SYSTEMS.map(s => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fid}-code`}>Code</label>
            <div className={styles.validateRow}>
              <input
                id={`${fid}-code`}
                className={styles.input}
                value={code}
                onChange={e => { setCode(e.target.value); setValid('idle') }}
                onBlur={validateCode}
                placeholder="e.g. 2524-7"
                style={{ flex: 1 }}
              />
              <button type="button" className={styles.actionBtn} onClick={validateCode}>
                Validate
              </button>
              <span className={styles.validateHint}>
                {valid === 'checking' && '…'}
                {valid === 'valid' && <span className={styles.valid}>✓ {display}</span>}
                {valid === 'invalid' && <span className={styles.invalid}>✗ unknown code</span>}
              </span>
            </div>
          </div>
        </div>
      )}

      {medicineFields ? (
        <>
          <div className={styles.formRow}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${fid}-drug`}>Medicine</label>
              <input
                id={`${fid}-drug`}
                className={styles.input}
                value={drug}
                onChange={e => setDrug(e.target.value)}
                placeholder="e.g. Ceftriaxone"
                autoComplete="off"
              />
            </div>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${fid}-dose`}>Dose</label>
              <input
                id={`${fid}-dose`}
                className={styles.input}
                value={dose}
                onChange={e => setDose(e.target.value)}
                placeholder="e.g. 1 g"
                autoComplete="off"
              />
            </div>
          </div>
          <div className={styles.formRow}>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${fid}-route`}>Route</label>
              <input
                id={`${fid}-route`}
                className={styles.input}
                value={route}
                onChange={e => setRoute(e.target.value)}
                placeholder="e.g. IV"
                autoComplete="off"
              />
            </div>
            <div className={styles.field}>
              <label className={styles.label} htmlFor={`${fid}-frequency`}>How often</label>
              <input
                id={`${fid}-frequency`}
                className={styles.input}
                value={frequency}
                onChange={e => setFrequency(e.target.value)}
                placeholder="e.g. once daily"
                autoComplete="off"
              />
            </div>
          </div>
          <div className={styles.formRow}>
            <label className={styles.label} style={{ display: 'flex', alignItems: 'center', gap: 8, minHeight: 24 }}>
              <input
                type="checkbox"
                checked={asNeeded}
                onChange={e => setAsNeeded(e.target.checked)}
              />
              Give only when needed
            </label>
            {asNeeded && (
              <div className={styles.field}>
                <label className={styles.label} htmlFor={`${fid}-prn`}>Give when</label>
                <input
                  id={`${fid}-prn`}
                  className={styles.input}
                  value={asNeededFor}
                  onChange={e => setAsNeededFor(e.target.value)}
                  placeholder="e.g. nausea"
                  autoComplete="off"
                />
              </div>
            )}
          </div>
        </>
      ) : (
        <div className={styles.formRow}>
          <div className={styles.field}>
            <label className={styles.label} htmlFor={`${fid}-description`}>Description</label>
            <input
              id={`${fid}-description`}
              className={styles.input}
              value={description}
              onChange={e => setDescription(e.target.value)}
              placeholder="Order description"
            />
          </div>
        </div>
      )}

      {isMedicationOrder(orderType) && (
        <div className={styles.medNotice} role="note">{medicationRoutingNotice(lifecycleOn)}</div>
      )}

      {error && <div className={styles.errorText}>{error}</div>}

      <div className={styles.formActions}>
        <button className={styles.cancelBtn} onClick={onCancel}>Cancel</button>
        <button className={styles.submitBtn} disabled={!canSubmit || submitting} onClick={() => submit()}>
          {submitting ? 'Creating…' : 'Create Order'}
        </button>
      </div>

      {safetyModal && (
        <SafetyAlertModal
          safety={safetyModal.safety}
          mode={safetyModal.mode}
          drugName={medicineFields ? drug.trim() : description}
          busy={submitting}
          onAcknowledge={(reason) => submit({ acknowledged: true, reason })}
          placedNotice={safetyModal.mode === 'info'
            ? (medicineFields
              ? 'This order has been placed as a draft. It reaches the ward only once a prescriber signs it. Cancel it if you did not intend it.'
              : 'This order has been placed as a draft. Cancel it if you did not intend it.')
            : undefined}
          onCancelOrder={safetyModal.mode === 'info' && safetyModal.orderId
            ? () => { void cancelPlacedOrder(safetyModal.orderId as string) }
            : undefined}
          cancelBusy={cancelling}
          cancelError={cancelError}
          onClose={() => {
            const wasInfo = safetyModal.mode === 'info'
            setSafetyModal(null)
            // Info mode means the order already exists — closing the review
            // finishes the create flow (blocking-cancel leaves the form up).
            if (wasInfo) onCreated()
          }}
        />
      )}
    </div>
  )
}
