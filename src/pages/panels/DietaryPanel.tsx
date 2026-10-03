/**
 * Orb — Diet & Kitchen.
 *
 * ╔════════════════════════════════════════════════════════════════════════╗
 * ║ NIL BY MOUTH IS THE FIRST THING ON THIS SCREEN, ALWAYS.                ║
 * ╚════════════════════════════════════════════════════════════════════════╝
 *
 * The ward view opens with an NPO block ABOVE the diet list, not with a filter
 * the reader has to apply. That is the whole design decision of this panel and
 * it is worth the duplication: an NPO patient rendered as one row among thirty
 * is an NPO patient who gets handed breakfast at 07:00 with a trolley in the
 * corridor. Each NPO card carries three things a paper diet sheet answers worst
 * — WHO ordered it, UNTIL WHEN, and WHY — and an expired window says "review
 * now" in words rather than as a negative number somebody has to interpret.
 *
 * NPO is never signalled by colour alone. Every NPO card carries the literal
 * words "NIL BY MOUTH" and every expired one carries "Window passed — review
 * now", so a colour-blind nurse and a monochrome ward printout read the same
 * thing as everybody else.
 *
 * ALLERGIES ARE THE SERVER'S LIVE READ, AND AN UNREADABLE ONE SAYS SO. This
 * panel never renders "No known allergies" from an empty array on its own: it
 * checks `allergies_unavailable` first, because an empty list and a failed read
 * look identical and mean opposite things. That check is the fail-closed half of
 * the drug interlock's posture, carried onto the tray card.
 *
 * COUNTS ARE SHOWN BESIDE THE SERVER'S OWN TOTAL. The panel sums the lines
 * itself (`sumLines`) and, if that disagrees with the total the server stored, it
 * SAYS SO rather than picking one. A count screen that quietly resolves its own
 * contradiction is a count screen nobody can audit.
 *
 * NO MONEY. Not one amount crosses this panel, so nothing here goes near
 * src/lib/money.ts. Everything is a whole-number count of trays.
 */
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  buildMealPlan, cancelIndent, DietaryApiError, discontinueDietOrder, getIndents,
  getKitchenBoard, getMealPlans, getMeta, getStaleOrders, getSummary, getWards,
  getWardView, issueIndent, lockMealPlan, mealServiceForHour, npoWindowLabel, raiseIndent,
  saveDietOrder, sumLines,
  type CountLine, type DietaryMeta, type DietaryScope, type DietarySummary, type DietOrder,
  type DietType, type KitchenBoard, type KitchenIndent, type MealPlan,
  type MealService, type Texture, type WardSummaryRow, type WardView,
} from '../../api/dietary'
import { useFocusTrap } from '../../hooks/useFocusTrap'
import { formatClinicalDate, localDateKey } from '../../lib/datetime'
import { humaniseEnum } from '../../lib/humanise'
import './DietaryPanel.css'

/* The sentence that must be on screen in the FIRST PAINT, before any network
 * round trip, and still there if every request fails. The one message that must
 * never be missing cannot be one that depends on the network being up. */
const NPO_FALLBACK_LABEL = 'NIL BY MOUTH'
const NPO_FALLBACK_NOTICE =
  'A nil-by-mouth window that has passed does NOT resume the diet automatically. '
  + 'A clinician must review and write the next order.'
const ALLERGY_FALLBACK_MESSAGE =
  'Allergies could not be read. This diet has NOT been checked against them. '
  + 'Do not assume the patient has no allergies.'

type View = 'ward' | 'kitchen' | 'plans' | 'lapsed'

const VIEWS: { id: View; label: string }[] = [
  { id: 'ward', label: 'Ward' },
  { id: 'kitchen', label: 'Kitchen' },
  { id: 'plans', label: 'Plans & indents' },
  { id: 'lapsed', label: 'Lapsed orders' },
]

const MEAL_SERVICE_ORDER: MealService[] = ['breakfast', 'lunch', 'dinner']

/* ── Small presentational pieces ─────────────────────────────────────────── */

function Tile({ value, caption, tone }: { value: string; caption: string; tone?: string }) {
  return (
    <div className="diet-tile">
      <div className={`diet-tile__value${tone ? ` diet-tile__value--${tone}` : ''}`}>{value}</div>
      <div className="diet-tile__caption">{caption}</div>
    </div>
  )
}

function Empty({ title, detail }: { title: string; detail: string }) {
  return (
    <div className="diet-empty">
      <div className="diet-empty__title">{title}</div>
      <div className="diet-empty__detail">{detail}</div>
    </div>
  )
}

function Badge({ tone, children }: { tone: string; children: ReactNode }) {
  return <span className={`diet-badge diet-badge--${tone}`}>{children}</span>
}

/** A service date (a calendar day with no time) in the clinical form. Built as
 *  a LOCAL date: read as a UTC instant it would print the day before west of
 *  Greenwich. */
function serviceDay(iso: string): string {
  const [y, m, d] = iso.split('-').map(Number)
  return y && m && d ? formatClinicalDate(new Date(y, m - 1, d)) : iso
}

/** Whose wards the tiles count. The tiles follow the caller's ward scope while
 *  the Kitchen tab counts every ward, and nothing on screen used to say which
 *  was which. */
function scopeCaption(scope: DietaryScope): string {
  if (scope.hospital_wide) return 'Counts for every ward.'
  if (scope.wards.length === 0) {
    return `Nobody is admitted under ${scope.department || 'your department'} right now, `
      + 'so there is no ward to count. The Kitchen tab counts every ward.'
  }
  return `Your wards: ${scope.wards.join(', ')}. The Kitchen tab counts every ward.`
}

/** Why a ward list is empty, in the reader's terms rather than as a blank picker. */
function noWardDetail(scope: DietaryScope | undefined): string {
  if (scope && !scope.hospital_wide) {
    return `Diets are listed by the ward a patient is admitted to, and nobody is `
      + `admitted under ${scope.department || 'your department'} right now. `
      + 'The Kitchen tab still counts every ward.'
  }
  return 'Nobody is admitted to any ward right now.'
}

/**
 * The allergy line for one patient.
 *
 * Order of the branches is the safety property: UNAVAILABLE is checked before
 * EMPTY. Swapping them turns a failed read into the words "No known allergies",
 * which is the single most dangerous sentence this panel could print.
 */
function Allergies({ order }: { order: DietOrder }) {
  if (order.allergies_unavailable) {
    return (
      <div className="diet-allergy diet-allergy--unknown">
        <span className="diet-allergy__flag">ALLERGIES UNKNOWN</span>
        <span>{order.allergies_message || ALLERGY_FALLBACK_MESSAGE}</span>
      </div>
    )
  }
  if (order.allergies.length === 0) {
    return <div className="diet-allergy diet-allergy--none">No allergies recorded</div>
  }
  return (
    <div className="diet-allergy diet-allergy--present">
      <span className="diet-allergy__flag">ALLERGIES</span>
      <span>
        {order.allergies.map((a) => (
          <span key={a.id} className="diet-allergy__item">
            {a.substance}
            {a.reaction ? ` (${a.reaction})` : ''}
            {a.severity === 'severe' ? ', severe' : ''}
          </span>
        ))}
      </span>
    </div>
  )
}

/** One NPO patient, rendered as a card and not as a table row. */
function NpoCard({ order, onStop }: { order: DietOrder; onStop?: (o: DietOrder) => void }) {
  const expired = order.npo_expired || order.npo_end_unknown
  return (
    <li className={`diet-npo${expired ? ' diet-npo--expired' : ''}`}>
      <div className="diet-npo__head">
        <span className="diet-npo__label">{order.npo_label || NPO_FALLBACK_LABEL}</span>
        <span className="diet-npo__who">
          {order.patient_name}
          {order.bed ? ` · Bed ${order.bed}` : ''}
        </span>
      </div>
      <div className="diet-npo__grid">
        <div className="diet-npo__cell">
          <span className="diet-npo__key">Until</span>
          <span className="diet-npo__val">{npoWindowLabel(order)}</span>
        </div>
        <div className="diet-npo__cell">
          <span className="diet-npo__key">Ordered by</span>
          <span className="diet-npo__val">{order.ordered_by_display}</span>
        </div>
        <div className="diet-npo__cell">
          <span className="diet-npo__key">Reason</span>
          <span className="diet-npo__val">{order.npo_reason || 'Not recorded'}</span>
        </div>
      </div>
      {order.npo_notice ? <p className="diet-npo__notice">{order.npo_notice}</p> : null}
      {order.npo_long_window && !expired ? (
        <p className="diet-npo__notice">
          This fast is longer than 48 hours. Confirm it is still intended.
        </p>
      ) : null}
      <Allergies order={order} />
      {onStop ? (
        <div className="diet-npo__actions">
          <button type="button" className="diet-btn diet-btn--tiny" onClick={() => onStop(order)}>
            Review / change diet
          </button>
        </div>
      ) : null}
    </li>
  )
}

function CountTable({ lines, total, agree }: {
  lines: CountLine[]; total: number; agree?: boolean
}) {
  const derived = sumLines(lines)
  return (
    <div className="diet-table-wrap">
      <table className="diet-table">
        <thead>
          <tr>
            <th scope="col">Diet</th>
            <th scope="col">Texture</th>
            <th scope="col" className="diet-num">Trays</th>
          </tr>
        </thead>
        <tbody>
          {lines.length === 0 ? (
            <tr><td colSpan={3}>No trays counted for this service.</td></tr>
          ) : lines.map((line) => (
            <tr key={`${line.diet_type}-${line.texture}`}>
              <td className="diet-cell__primary">{line.diet_label}</td>
              <td>{line.texture_label}</td>
              <td className="diet-num">{line.meal_count}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="diet-table__total">
            <th scope="row" colSpan={2}>Total trays</th>
            <td className="diet-num">{total}</td>
          </tr>
        </tfoot>
      </table>
      {derived !== total || agree === false ? (
        <p className="diet-alert diet-alert--danger">
          This count does not add up: the lines total {derived} but the plan says {total}.
          Do not cook against it. Rebuild the plan.
        </p>
      ) : null}
    </div>
  )
}

/* ── The order drawer ────────────────────────────────────────────────────── */

interface DrawerState {
  patientId: string
  patientName: string
  existing: DietOrder | null
}

function OrderDrawer({
  state, meta, busy, serverError, onClose, onSave, onDiscontinue,
}: {
  state: DrawerState
  meta: DietaryMeta | null
  busy: boolean
  /** The server's refusal of the last save, shown here because the panel's own
   *  banner sits behind this drawer's scrim. */
  serverError: string | null
  onClose: () => void
  onSave: (input: {
    patient_id: string; diet_type: DietType; texture: Texture
    restriction_notes?: string; notes?: string
    npo_reason?: string; npo_until?: string
  }) => void
  onDiscontinue: (order: DietOrder) => void
}) {
  const existing = state.existing
  const [dietType, setDietType] = useState<DietType>(existing?.diet_type ?? 'normal')
  const [texture, setTexture] = useState<Texture>(existing?.texture ?? 'regular')
  const [restrictions, setRestrictions] = useState(existing?.restriction_notes ?? '')
  const [notes, setNotes] = useState(existing?.notes ?? '')
  const [npoReason, setNpoReason] = useState(existing?.npo_reason ?? '')
  const [npoUntil, setNpoUntil] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  const panelRef = useFocusTrap<HTMLDivElement>(onClose)
  const isNpo = dietType === (meta?.npo_diet_type ?? 'npo')
  const dietTypes = meta?.diet_types ?? ['normal', 'diabetic', 'renal', 'cardiac', 'soft', 'liquid', 'npo']
  const textures = meta?.textures ?? ['regular', 'soft', 'minced', 'pureed', 'liquid', 'thickened']

  const submit = () => {
    setLocalError(null)
    if (isNpo) {
      /* Checked here as well as on the server, so the clinician is told AT THE
       * FIELD rather than by a 400 after they have already left the form. The
       * server refuses it regardless — this is a courtesy, never the guard. */
      if (!npoReason.trim()) {
        setLocalError('A nil-by-mouth order needs a reason, "for OT 08:00", "aspiration risk".')
        return
      }
      if (!npoUntil) {
        setLocalError('A nil-by-mouth order needs an end time. Nil by mouth with no end is how a patient stays starved after a cancelled list.')
        return
      }
    }
    onSave({
      patient_id: state.patientId,
      diet_type: dietType,
      texture: isNpo ? 'regular' : texture,
      restriction_notes: restrictions.trim() || undefined,
      notes: notes.trim() || undefined,
      npo_reason: isNpo ? npoReason.trim() : undefined,
      npo_until: isNpo ? npoUntil : undefined,
    })
  }

  /* Portalled to document.body — a drawer nested inside the panel would be
   * clipped by the panel's own stacking context and scroll container. */
  return createPortal(
    <div className="diet-drawer" role="dialog" aria-modal="true" aria-label="Diet order">
      <button type="button" className="diet-drawer__scrim" aria-label="Close" onClick={onClose} />
      <div className="diet-drawer__panel" ref={panelRef}>
        <div className="diet-drawer__head">
          <div>
            <h3 className="diet-drawer__title">Diet order</h3>
            <p className="diet-drawer__sub">
              {state.patientName}
              {existing ? ` · currently ${existing.diet_label}` : ' · no current order'}
            </p>
          </div>
          <button type="button" className="diet-btn diet-btn--tiny" onClick={onClose}>Close</button>
        </div>

        <div className="diet-drawer__body">
          {existing ? <Allergies order={existing} /> : null}

          <div className="diet-form-grid">
            <label className="diet-field">
              <span className="diet-field__label">Diet type</span>
              <select
                className="diet-input"
                value={dietType}
                onChange={(e) => setDietType(e.target.value as DietType)}
              >
                {dietTypes.map((id) => (
                  <option key={id} value={id}>
                    {meta?.diet_type_labels?.[id] ?? id}
                  </option>
                ))}
              </select>
            </label>

            {!isNpo ? (
              <label className="diet-field">
                <span className="diet-field__label">Texture</span>
                <select
                  className="diet-input"
                  value={texture}
                  onChange={(e) => setTexture(e.target.value as Texture)}
                >
                  {textures.map((id) => (
                    <option key={id} value={id}>{meta?.texture_labels?.[id] ?? id}</option>
                  ))}
                </select>
              </label>
            ) : null}
          </div>

          {isNpo ? (
            <div className="diet-npo-form">
              <p className="diet-npo-form__banner">
                {NPO_FALLBACK_LABEL}. This patient will get no tray. Both fields below
                are required.
              </p>
              <div className="diet-form-grid">
                <label className="diet-field">
                  <span className="diet-field__label">Nil by mouth until</span>
                  <input
                    type="datetime-local"
                    className="diet-input"
                    value={npoUntil}
                    onChange={(e) => setNpoUntil(e.target.value)}
                  />
                  <span className="diet-field__hint">
                    Hospital local time. {meta?.npo_expiry_notice || NPO_FALLBACK_NOTICE}
                  </span>
                </label>
                <label className="diet-field">
                  <span className="diet-field__label">Reason</span>
                  <input
                    className="diet-input"
                    value={npoReason}
                    placeholder="for OT 08:00"
                    onChange={(e) => setNpoReason(e.target.value)}
                  />
                </label>
              </div>
            </div>
          ) : (
            <label className="diet-field">
              <span className="diet-field__label">Dietary restrictions</span>
              <input
                className="diet-input"
                value={restrictions}
                placeholder="low salt, vegetarian, 1.2 L fluid restriction"
                onChange={(e) => setRestrictions(e.target.value)}
              />
              <span className="diet-field__hint">
                Dietary restrictions only. Allergies are read from the patient's allergy
                record and are never re-typed here.
              </span>
            </label>
          )}

          <label className="diet-field">
            <span className="diet-field__label">Notes for the kitchen</span>
            <input className="diet-input" value={notes} onChange={(e) => setNotes(e.target.value)} />
          </label>

          {localError ? <p className="diet-alert diet-alert--danger">{localError}</p> : null}
          {serverError && !localError ? (
            <p className="diet-alert diet-alert--danger" role="alert">{serverError}</p>
          ) : null}

          <div className="diet-drawer__actions">
            <button
              type="button"
              className="diet-btn diet-btn--primary"
              disabled={busy}
              onClick={submit}
            >
              {existing ? 'Change diet order' : 'Write diet order'}
            </button>
            {existing ? (
              <button
                type="button"
                className="diet-btn"
                disabled={busy}
                onClick={() => onDiscontinue(existing)}
              >
                Discontinue order
              </button>
            ) : null}
          </div>
        </div>
      </div>
    </div>,
    document.body,
  )
}

/* ── The panel ───────────────────────────────────────────────────────────── */

export default function DietaryPanel() {
  const [view, setView] = useState<View>('ward')
  const [meta, setMeta] = useState<DietaryMeta | null>(null)
  const [summary, setSummary] = useState<DietarySummary | null>(null)
  /* null means "not loaded yet", never "none": every list below starts null so
     the screen says Loading rather than "No meal plans yet" before it has asked. */
  const [wards, setWards] = useState<WardSummaryRow[] | null>(null)
  const [ward, setWard] = useState<string>('')
  const [wardView, setWardView] = useState<WardView | null>(null)
  const [kitchen, setKitchen] = useState<KitchenBoard | null>(null)
  const [plansData, setPlansData] = useState<{
    key: string; plans: MealPlan[]; indents: KitchenIndent[]
  } | null>(null)
  const [showEarlierDays, setShowEarlierDays] = useState(false)
  const [lapsed, setLapsed] = useState<DietOrder[] | null>(null)
  /* The service the ward is counting for now. The browser's hour is only the
     first guess; /meta replaces it with the hospital's own unless the reader
     has already picked one. It used to open on Lunch at any hour. */
  const [mealService, setMealService] = useState<MealService>(
    () => mealServiceForHour(new Date().getHours()),
  )
  const servicePicked = useRef(false)
  const [drawer, setDrawer] = useState<DrawerState | null>(null)
  const [moduleOff, setModuleOff] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [reloadToken, setReloadToken] = useState(0)

  const reload = useCallback(() => setReloadToken((n) => n + 1), [])

  /** One place every failure lands, so a switched-off module can never be
   *  mistaken for a ward with no diet orders. */
  const handle = useCallback((err: unknown) => {
    if (err instanceof DietaryApiError && err.isModuleDisabled) {
      setModuleOff(err.message)
      return
    }
    setError(err instanceof Error ? err.message : String(err))
  }, [])

  /** Resolves true when the action went through. The order drawer stays open on
   *  false, so a refused save does not throw away what the clinician typed. */
  const run = useCallback(async (fn: () => Promise<unknown>, note?: string): Promise<boolean> => {
    setBusy(true)
    setError(null)
    try {
      await fn()
      if (note) setFlash(note)
      reload()
      return true
    } catch (err) {
      handle(err)
      return false
    } finally {
      setBusy(false)
    }
  }, [handle, reload])

  useEffect(() => {
    let alive = true
    getMeta().then((m) => {
      if (!alive) return
      setMeta(m)
      const now = m.current_meal_service
      if (now && !servicePicked.current) setMealService(now)
    }).catch(handle)
    return () => { alive = false }
  }, [handle])

  const pickService = (next: MealService) => {
    servicePicked.current = true
    setMealService(next)
  }

  const openDrawer = (next: DrawerState) => {
    // A refusal from an earlier action must not read as this order's.
    setError(null)
    setDrawer(next)
  }

  useEffect(() => {
    let alive = true
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setLoading(true)
    Promise.all([getSummary(), getWards()])
      .then(([s, w]) => {
        if (!alive) return
        setSummary(s)
        setWards(w)
        if (!ward && w.length > 0) setWard(w[0].ward)
      })
      .catch(handle)
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken, handle])

  useEffect(() => {
    if (view !== 'ward') return
    let alive = true
    getWardView(ward || undefined)
      .then((v) => { if (alive) setWardView(v) })
      .catch(handle)
    return () => { alive = false }
  }, [view, ward, reloadToken, handle])

  useEffect(() => {
    if (view !== 'kitchen') return
    let alive = true
    getKitchenBoard({ meal_service: mealService })
      .then((b) => { if (alive) setKitchen(b) })
      .catch(handle)
    return () => { alive = false }
  }, [view, mealService, reloadToken, handle])

  /* Plans and indents follow the Ward and Service pickers above them, and today
     by default. They used to be fetched with no filter at all, so a GM-1 desk
     listed CCU plans and month-old drafts with Issue and Cancel beside them.
     "Today" is the hospital's day from /meta; the browser's is only a fallback. */
  const planWard = ward || wards?.[0]?.ward || ''
  const serviceDate = meta?.service_date || localDateKey()
  const plansKey = `${planWard}|${mealService}|${showEarlierDays ? 'all' : serviceDate}`
  useEffect(() => {
    if (view !== 'plans' || !planWard) return
    let alive = true
    const params = {
      ward: planWard,
      meal_service: mealService,
      service_date: showEarlierDays ? undefined : serviceDate,
    }
    Promise.all([getMealPlans(params), getIndents(params)])
      .then(([p, i]) => {
        if (alive) setPlansData({ key: plansKey, plans: p, indents: i })
      })
      .catch(handle)
    return () => { alive = false }
    // plansKey carries planWard, mealService, serviceDate and the toggle.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view, plansKey, reloadToken, handle])

  useEffect(() => {
    if (view !== 'lapsed') return
    let alive = true
    getStaleOrders().then((rows) => { if (alive) setLapsed(rows) }).catch(handle)
    return () => { alive = false }
  }, [view, reloadToken, handle])

  const npoLabel = meta?.npo_label || summary?.npo_label || NPO_FALLBACK_LABEL
  /* NOTE: the server's `npo_expiry_notice` is deliberately NOT read here. It is
     wording about ONE lapsed window and belongs on the card for that order,
     where NpoCard reads it off `order.npo_notice`. */

  const mealRows = useMemo(
    () => (wardView?.rows ?? []).filter((r) => !r.is_npo),
    [wardView],
  )

  /* Hide what the server would refuse: a catering account was offered Change on
     every diet and met a 403. Advisory only. The routes still decide, and an
     answer that has not arrived (or an older server) leaves the buttons up. */
  const canWriteOrders = meta?.capabilities?.write_orders !== false
  const canRunKitchen = meta?.capabilities?.run_kitchen !== false
  const noWards = wards !== null && wards.length === 0
  /* Plans for another filter are not shown while this one loads: they would be
     a stale answer to a different question. */
  const plans = plansData?.key === plansKey ? plansData.plans : null
  const indents = plansData?.key === plansKey ? plansData.indents : null

  if (moduleOff) {
    return (
      <div className="diet-panel">
        <header className="diet-head">
          <div>
            <h2 className="diet-title">Diet &amp; Kitchen</h2>
            <p className="diet-sub">Not available at this site.</p>
          </div>
        </header>
        <p className="diet-notice diet-notice--off">{moduleOff}</p>
      </div>
    )
  }

  return (
    <div className="diet-panel">
      <header className="diet-head">
        <div>
          <h2 className="diet-title">Diet &amp; Kitchen</h2>
          <p className="diet-sub">
            Diet orders, ward meal counts and the kitchen indent. A patient who is
            discharged leaves the count automatically.
          </p>
        </div>
      </header>

      {/* First paint, no network needed. The one sentence that must never be
          missing cannot be one that depends on a fetch. */}
      {/* THE RULE, NOT A CLAIM ABOUT NOW. This banner is always on screen, so it
          must be true on a ward where nobody is nil by mouth. It used to append
          the server's `npo_expiry_notice`, which is written demonstratively
          ("THIS NPO window has passed…") because its real home is a card about
          one specific order — and NpoCard still renders it there, off
          `order.npo_notice`, which the server sets only when that window really
          has passed. Pasted into a standing header it told a clinician a window
          had lapsed on a ward showing "0 NIL BY MOUTH now". */}
      <p className="diet-notice">
        <strong>{npoLabel}</strong> is shown separately on every ward, with who ordered
        it and until when. {NPO_FALLBACK_NOTICE}
      </p>

      {summary ? (
        <div className="diet-tiles">
          <Tile value={String(summary.active_orders)} caption="Active diet orders" />
          <Tile value={String(summary.meals)} caption="Patients being fed" />
          <Tile
            value={String(summary.npo_active)}
            caption={`${npoLabel} now`}
            tone={summary.npo_active > 0 ? 'warn' : undefined}
          />
          <Tile
            value={String(summary.npo_needs_review)}
            caption="Nil-by-mouth windows passed: review"
            tone={summary.npo_needs_review > 0 ? 'danger' : undefined}
          />
          <Tile
            value={String(summary.admitted_without_order)}
            caption="Admitted, no diet order"
            tone={summary.admitted_without_order > 0 ? 'warn' : undefined}
          />
          <Tile
            value={String(summary.stale_order_count)}
            caption="Orders left by a discharge"
          />
        </div>
      ) : null}
      {summary?.scope ? <p className="diet-hint">{scopeCaption(summary.scope)}</p> : null}

      <nav className="diet-tabs" aria-label="Diet views">
        {VIEWS.map((v) => (
          <button
            key={v.id}
            type="button"
            className={`diet-tab${view === v.id ? ' diet-tab--on' : ''}`}
            aria-current={view === v.id ? 'page' : undefined}
            onClick={() => setView(v.id)}
          >
            {v.label}
          </button>
        ))}
      </nav>

      {error ? <p className="diet-alert diet-alert--danger">{error}</p> : null}
      {flash ? <p className="diet-alert diet-alert--ok">{flash}</p> : null}
      {loading && !summary ? <p className="diet-loading">Loading…</p> : null}

      {view === 'ward' && noWards ? (
        <section className="diet-stack">
          <Empty title="No ward to show" detail={noWardDetail(summary?.scope)} />
        </section>
      ) : null}

      {view === 'ward' && !noWards ? (
        <section className="diet-stack">
          <div className="diet-section__header">
            <label className="diet-field diet-field--inline">
              <span className="diet-field__label">Ward</span>
              <select
                className="diet-input"
                value={ward}
                onChange={(e) => setWard(e.target.value)}
              >
                <option value="">All wards in my scope</option>
                {(wards ?? []).map((w) => (
                  <option key={w.ward} value={w.ward}>
                    {w.ward} · {w.with_orders}/{w.admitted} ordered
                  </option>
                ))}
              </select>
            </label>
          </div>
          {!canWriteOrders ? (
            <p className="diet-hint">
              Diet orders are written by doctors and nurses. This account can read them.
            </p>
          ) : null}

          {wardView === null ? <p className="diet-loading">Loading the ward…</p> : (
            <>
              {wardView?.allergies_unavailable ? (
                <p className="diet-alert diet-alert--danger">
                  {wardView.allergies_message || ALLERGY_FALLBACK_MESSAGE}
                </p>
              ) : null}

              {/* NPO FIRST. Above the diet list, never inside it. */}
              <section className="diet-section">
                <h3 className="diet-section__title">
                  {npoLabel}: {wardView?.npo_count ?? 0} patient
                  {(wardView?.npo_count ?? 0) === 1 ? '' : 's'}
                </h3>
                {(wardView?.npo_rows.length ?? 0) === 0 ? (
                  <Empty
                    title={`No patient on this ward is ${npoLabel.toLowerCase()}`}
                    detail="Every admitted patient with a diet order is being fed."
                  />
                ) : (
                  <ul className="diet-npo-list">
                    {wardView!.npo_rows.map((order) => (
                      <NpoCard
                        key={order.id}
                        order={order}
                        onStop={canWriteOrders ? (o) => openDrawer({
                          patientId: o.patient_id, patientName: o.patient_name, existing: o,
                        }) : undefined}
                      />
                    ))}
                  </ul>
                )}
              </section>

              <section className="diet-section">
                <h3 className="diet-section__title">Diets on this ward</h3>
                {mealRows.length === 0 ? (
                  <Empty
                    title="No diet orders here"
                    detail="Write one from the list of admitted patients below."
                  />
                ) : (
                  <div className="diet-table-wrap">
                    <table className="diet-table">
                      <thead>
                        <tr>
                          <th scope="col">Bed</th>
                          <th scope="col">Patient</th>
                          <th scope="col">Diet</th>
                          <th scope="col">Texture</th>
                          <th scope="col">Allergies</th>
                          <th scope="col">Ordered by</th>
                          <th scope="col" />
                        </tr>
                      </thead>
                      <tbody>
                        {mealRows.map((order) => (
                          <tr key={order.id}>
                            <td className="diet-num">{order.bed || '—'}</td>
                            <td className="diet-cell__primary">
                              {order.patient_name}
                              {order.restriction_notes ? (
                                <div className="diet-cell__secondary">{order.restriction_notes}</div>
                              ) : null}
                            </td>
                            <td>{order.diet_label}</td>
                            <td>{order.texture_label}</td>
                            <td><Allergies order={order} /></td>
                            <td>{order.ordered_by_display}</td>
                            <td className="diet-actions">
                              {canWriteOrders ? (
                                <button
                                  type="button"
                                  className="diet-btn diet-btn--tiny"
                                  onClick={() => openDrawer({
                                    patientId: order.patient_id,
                                    patientName: order.patient_name,
                                    existing: order,
                                  })}
                                >
                                  Change
                                </button>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>

              <section className="diet-section">
                <h3 className="diet-section__title">
                  Admitted with no diet order: {wardView?.unordered_count ?? 0}
                </h3>
                {(wardView?.unordered.length ?? 0) === 0 ? (
                  <Empty
                    title="Everybody on this ward has a diet order"
                    detail="Nothing to write."
                  />
                ) : (
                  <ul className="diet-list">
                    {wardView!.unordered.map((p) => (
                      <li key={p.patient_id} className="diet-row">
                        <div className="diet-row__body">
                          <div className="diet-row__primary">{p.patient_name}</div>
                          <div className="diet-row__secondary">
                            Bed {p.bed || '—'} · {p.patient_mrn || 'no MRN'}
                          </div>
                        </div>
                        {canWriteOrders ? (
                          <button
                            type="button"
                            className="diet-btn diet-btn--tiny"
                            onClick={() => openDrawer({
                              patientId: p.patient_id, patientName: p.patient_name, existing: null,
                            })}
                          >
                            Write diet order
                          </button>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </section>
      ) : null}

      {view === 'kitchen' ? (
        <section className="diet-stack">
          <div className="diet-section__header">
            <label className="diet-field diet-field--inline">
              <span className="diet-field__label">Service</span>
              <select
                className="diet-input"
                value={mealService}
                onChange={(e) => pickService(e.target.value as MealService)}
              >
                {MEAL_SERVICE_ORDER.map((id) => (
                  <option key={id} value={id}>
                    {meta?.meal_service_labels?.[id] ?? id}
                  </option>
                ))}
              </select>
            </label>
            <p className="diet-hint">
              Counts only: this screen carries no patient names, beds or allergies.
            </p>
          </div>

          {kitchen ? (
            <>
              <div className="diet-tiles">
                <Tile value={String(kitchen.hospital_total_meals)} caption="Trays, all wards" />
                <Tile
                  value={String(kitchen.hospital_npo_count)}
                  caption={`${npoLabel}, no tray`}
                  tone={kitchen.hospital_npo_count > 0 ? 'warn' : undefined}
                />
                <Tile value={serviceDay(kitchen.service_date)} caption="Service date" />
              </div>

              {kitchen.wards.length === 0 ? (
                <Empty
                  title="No diet orders anywhere"
                  detail="Nothing to cook. Wards write diet orders from the Ward view."
                />
              ) : kitchen.wards.map((w) => (
                <section key={w.ward} className="diet-section">
                  <h3 className="diet-section__title">
                    {w.ward}: {w.total_meals} trays
                    {w.npo_count > 0 ? `, ${w.npo_count} ${npoLabel.toLowerCase()}` : ''}
                  </h3>
                  <CountTable lines={w.lines} total={w.total_meals} />
                </section>
              ))}

              {kitchen.plans.length > 0 ? (
                <section className="diet-section">
                  <h3 className="diet-section__title">Plans for this service</h3>
                  <ul className="diet-list">
                    {kitchen.plans.map((p) => (
                      <li
                        key={p.id}
                        className={`diet-row${p.drift !== 0 ? ' diet-row--warn' : ''}`}
                      >
                        <div className="diet-row__body">
                          <div className="diet-row__primary">
                            {p.ward} · {p.meal_service_label}
                          </div>
                          <div className="diet-row__secondary">
                            {p.total_meals} trays counted
                            {p.drift !== 0
                              ? ` · ward has moved by ${p.drift > 0 ? '+' : ''}${p.drift} since`
                              : ' · unchanged since counted'}
                          </div>
                        </div>
                        <div className="diet-row__trailing">
                          <Badge tone={p.status === 'locked' ? 'ok' : 'neutral'}>{humaniseEnum(p.status)}</Badge>
                          {p.indent ? (
                            <span className="diet-row__note">
                              Indent {p.indent.indent_number || p.indent.id} · {humaniseEnum(p.indent.status).toLowerCase()}
                            </span>
                          ) : null}
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              ) : null}
            </>
          ) : <p className="diet-loading">Loading kitchen counts…</p>}
        </section>
      ) : null}

      {view === 'plans' ? (
        <section className="diet-stack">
          {noWards ? (
            <Empty title="No ward to count" detail={noWardDetail(summary?.scope)} />
          ) : (
            <div className="diet-section__header">
              <div className="diet-form-grid">
                <label className="diet-field diet-field--inline">
                  <span className="diet-field__label">Ward</span>
                  <select
                    className="diet-input"
                    value={planWard}
                    onChange={(e) => setWard(e.target.value)}
                  >
                    {(wards ?? []).map((w) => <option key={w.ward} value={w.ward}>{w.ward}</option>)}
                  </select>
                </label>
                <label className="diet-field diet-field--inline">
                  <span className="diet-field__label">Service</span>
                  <select
                    className="diet-input"
                    value={mealService}
                    onChange={(e) => pickService(e.target.value as MealService)}
                  >
                    {MEAL_SERVICE_ORDER.map((id) => (
                      <option key={id} value={id}>{meta?.meal_service_labels?.[id] ?? id}</option>
                    ))}
                  </select>
                </label>
              </div>
              <div className="diet-plan__actions">
                <button
                  type="button"
                  className="diet-btn"
                  onClick={() => setShowEarlierDays((on) => !on)}
                >
                  {showEarlierDays ? 'Show today only' : 'Show earlier days'}
                </button>
                {canRunKitchen ? (
                  <button
                    type="button"
                    className="diet-btn diet-btn--primary"
                    disabled={busy || !planWard}
                    onClick={() => run(
                      () => buildMealPlan({ ward: planWard, meal_service: mealService }),
                      'Meal plan built from the ward\'s live diet orders.',
                    )}
                  >
                    Build count for {planWard || 'ward'}
                  </button>
                ) : null}
              </div>
            </div>
          )}
          {!noWards && !canRunKitchen ? (
            <p className="diet-hint">
              This account can read counts and indents but cannot build, lock, issue or cancel them.
            </p>
          ) : null}

          {noWards ? null : plans === null ? (
            error ? null : <p className="diet-loading">Loading meal plans…</p>
          ) : plans.length === 0 ? (
            <Empty
              title={showEarlierDays ? 'No meal plans yet' : `No count for ${planWard} today`}
              detail="Build one for a ward and service. It counts the diet orders of the patients who are on the ward right now."
            />
          ) : (
            <ul className="diet-list">
              {plans.map((plan) => (
                <li key={plan.id} className="diet-plan">
                  <div className="diet-plan__head">
                    <div>
                      <div className="diet-row__primary">
                        {plan.ward} · {plan.meal_service_label} · {serviceDay(plan.service_date)}
                      </div>
                      <div className="diet-row__secondary">
                        {plan.total_meals} trays · {plan.npo_count} {npoLabel.toLowerCase()} ·
                        {' '}{plan.order_count} orders counted
                      </div>
                    </div>
                    <Badge tone={plan.status === 'locked' ? 'ok' : 'neutral'}>{humaniseEnum(plan.status)}</Badge>
                  </div>
                  <CountTable
                    lines={plan.lines}
                    total={plan.total_meals}
                    agree={plan.totals_agree}
                  />
                  <div className="diet-plan__actions">
                    {canRunKitchen && plan.status === 'draft' ? (
                      <button
                        type="button"
                        className="diet-btn"
                        disabled={busy}
                        onClick={() => run(
                          () => lockMealPlan(plan.id),
                          'Count locked. The kitchen can cook against it.',
                        )}
                      >
                        Lock this count
                      </button>
                    ) : null}
                    {canRunKitchen && plan.status === 'locked' && !plan.indent ? (
                      <button
                        type="button"
                        className="diet-btn diet-btn--primary"
                        disabled={busy}
                        onClick={() => run(() => raiseIndent(plan.id), 'Kitchen indent raised.')}
                      >
                        Raise kitchen indent
                      </button>
                    ) : null}
                    {plan.indent ? (
                      <span className="diet-row__note">
                        Indent {plan.indent.indent_number || plan.indent.id} · {humaniseEnum(plan.indent.status).toLowerCase()}
                      </span>
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {noWards ? null : (
            <section className="diet-section">
              <h3 className="diet-section__title">Kitchen indents</h3>
              {indents === null ? (
                error ? null : <p className="diet-loading">Loading kitchen indents…</p>
              ) : indents.length === 0 ? (
                <Empty
                  title="No indents raised"
                  detail="An indent is raised from a locked count and is what the kitchen cooks against."
                />
              ) : (
                <ul className="diet-list">
                  {indents.map((indent) => (
                    <li key={indent.id} className="diet-row">
                      <div className="diet-row__body">
                        <div className="diet-row__primary">
                          {indent.indent_number || `Draft #${indent.id}`} · {indent.ward} ·
                          {' '}{indent.meal_service_label}
                        </div>
                        <div className="diet-row__secondary">
                          {indent.total_meals} trays · {serviceDay(indent.service_date)}
                          {indent.number_series_is_configured === false
                            ? ' · placeholder number series'
                            : ''}
                        </div>
                      </div>
                      <div className="diet-row__trailing">
                        <Badge tone={indent.status === 'issued' ? 'ok' : 'neutral'}>
                          {humaniseEnum(indent.status)}
                        </Badge>
                        {canRunKitchen && indent.status === 'draft' ? (
                          <button
                            type="button"
                            className="diet-btn diet-btn--tiny"
                            disabled={busy}
                            onClick={() => run(
                              () => issueIndent(indent.id), 'Indent issued to the kitchen.',
                            )}
                          >
                            Issue
                          </button>
                        ) : null}
                        {canRunKitchen && indent.status !== 'cancelled' ? (
                          <button
                            type="button"
                            className="diet-btn diet-btn--tiny"
                            disabled={busy}
                            onClick={() => run(
                              () => cancelIndent(indent.id), 'Indent cancelled.',
                            )}
                          >
                            Cancel
                          </button>
                        ) : null}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          )}
        </section>
      ) : null}

      {view === 'lapsed' ? (
        <section className="diet-stack">
          <p className="diet-hint">
            These diet orders are still open but their patient is no longer admitted here.
            They are already OUT of every kitchen count. This list exists so somebody can
            close them, rather than the row quietly disappearing.
          </p>
          {lapsed === null ? (
            error ? null : <p className="diet-loading">Loading lapsed orders…</p>
          ) : lapsed.length === 0 ? (
            <Empty
              title="Nothing lapsed"
              detail="Every open diet order belongs to a patient who is still on a ward."
            />
          ) : (
            <ul className="diet-list">
              {lapsed.map((order) => (
                <li key={order.id} className="diet-row diet-row--warn">
                  <div className="diet-row__body">
                    <div className="diet-row__primary">
                      {order.patient_name} · {order.diet_label}
                    </div>
                    <div className="diet-row__secondary">
                      Was {order.ward || 'unknown ward'} bed {order.bed || '—'} ·
                      {' '}ordered by {order.ordered_by_display}
                    </div>
                  </div>
                  {canWriteOrders ? (
                    <button
                      type="button"
                      className="diet-btn diet-btn--tiny"
                      disabled={busy}
                      onClick={() => run(
                        () => discontinueDietOrder(order.id, 'Patient no longer admitted'),
                        'Order closed.',
                      )}
                    >
                      Close order
                    </button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      {drawer ? (
        <OrderDrawer
          state={drawer}
          meta={meta}
          busy={busy}
          serverError={error}
          onClose={() => setDrawer(null)}
          onSave={(input) => {
            run(() => saveDietOrder(input), 'Diet order written.')
              .then((ok) => { if (ok) setDrawer(null) })
          }}
          onDiscontinue={(order) => {
            run(() => discontinueDietOrder(order.id), 'Diet order discontinued.')
              .then((ok) => { if (ok) setDrawer(null) })
          }}
        />
      ) : null}
    </div>
  )
}
