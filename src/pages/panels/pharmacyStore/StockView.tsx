/**
 * Pharmacy store, Stock (grn-9): what is on the shelves and where, what needs
 * ordering, what is close to expiry, and the drug ledger.
 *
 * Discovered by the panel through ./viewRegistry.ts; shown to anyone holding
 * pharmacy_store.read. Everything here is read-only: stock moves only through
 * the dispense, receipts, transfers and counts, each with its own screen.
 *
 * THE SERVER'S BUCKETS ARE SHOWN AS THEY COME. Dispensable is what a dispense
 * could take today (the same rule the dispense uses); usable stock in a
 * sub-store has to be transferred to the shelf first; quarantined and expired
 * units are counted apart and never as stock to use. A value the server could
 * not work out is shown in its words ('Rate not configured', 'No reorder level
 * set'), never as 0.
 *
 * No patient data: a ledger row tied to a patient's medicine order shows only
 * that it was, never the order or its remarks. Calendar dates (expiry) go
 * through formatClinicalDateKey; ledger instants are stored UTC and go
 * through formatClinicalDateTime.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { toPharmacyStoreError, type PharmacyStoreMeta } from '../../../api/pharmacyStore'
import {
  getLedgerPage,
  getNearExpiry,
  getReorderReport,
  getStock,
  getStockOverview,
  listStockLots,
  type LedgerMovement,
  type NearExpiryReport,
  type ReorderRow,
  type StockLot,
  type StockOverview,
  type StockRow,
} from '../../../api/pharmacyStore/stock'
import { formatClinicalDateKey, formatClinicalDateTime } from '../../../lib/datetime'
import { formatInr } from '../../../lib/money'
import type { PharmacyStoreViewProps } from './viewRegistry'
import './stockAndPurchasing.css'

export const viewId = 'stock'
export const viewLabel = 'Stock'
export const viewOrder = 40
export const viewPermission = 'pharmacy_store.read'

type Section = 'stock' | 'reorder' | 'expiry' | 'ledger'

const SECTIONS: { id: Section; label: string }[] = [
  { id: 'stock', label: 'Stock' },
  { id: 'reorder', label: 'Reorder' },
  { id: 'expiry', label: 'Near expiry' },
  { id: 'ledger', label: 'Ledger' },
]

type Fail = (err: unknown) => string

function useFail(onModuleOff: () => void): Fail {
  return useCallback((err: unknown) => {
    const failure = toPharmacyStoreError(err)
    if (failure.isModuleDisabled) onModuleOff()
    return failure.message || 'Something went wrong.'
  }, [onModuleOff])
}

/** A ledger filter: one drug, one lot, or everything. */
interface LedgerFilter {
  drugId?: string
  lotId?: string
  label: string
}

const ALL_MOVEMENTS: LedgerFilter = { label: 'All movements' }

function nearExpiryDays(meta: PharmacyStoreMeta): number | null {
  const settings = (meta.settings ?? {}) as Record<string, unknown>
  const days = settings.near_expiry_days
  return typeof days === 'number' ? days : null
}

function drugLine(row: { drug_name: string; strength?: string | null; form?: string | null }): string {
  return [row.strength, row.form].filter(Boolean).join(', ')
}

export default function StockView({ meta, onModuleOff, openView }: PharmacyStoreViewProps) {
  const [section, setSection] = useState<Section>('stock')
  const [ledgerFilter, setLedgerFilter] = useState<LedgerFilter>(ALL_MOVEMENTS)
  const fail = useFail(onModuleOff)

  function showLedger(filter: LedgerFilter) {
    setLedgerFilter(filter)
    setSection('ledger')
  }

  return (
    <div className="psw-view">
      <OverviewTiles fail={fail} onSection={setSection} openView={openView} />
      <nav className="psw-sections" aria-label="Stock sections">
        {SECTIONS.map(s => (
          <button
            key={s.id}
            type="button"
            className={`psw-section${s.id === section ? ' psw-section--on' : ''}`}
            aria-current={s.id === section ? 'page' : undefined}
            onClick={() => {
              if (s.id === 'ledger') setLedgerFilter(ALL_MOVEMENTS)
              setSection(s.id)
            }}
          >
            {s.label}
          </button>
        ))}
      </nav>
      {section === 'stock' && <StockSection fail={fail} onLedger={showLedger} />}
      {section === 'reorder' && <ReorderSection fail={fail} openView={openView} />}
      {section === 'expiry' && <ExpirySection fail={fail} defaultDays={nearExpiryDays(meta)} />}
      {section === 'ledger' && (
        <LedgerSection
          key={`${ledgerFilter.drugId ?? ''}|${ledgerFilter.lotId ?? ''}`}
          fail={fail}
          filter={ledgerFilter}
          onClear={() => setLedgerFilter(ALL_MOVEMENTS)}
        />
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Overview tiles
// ═════════════════════════════════════════════════════════════════════════════

function OverviewTiles({ fail, onSection, openView }: {
  fail: Fail
  onSection: (s: Section) => void
  openView: (viewId: string) => void
}) {
  const [overview, setOverview] = useState<StockOverview | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    getStockOverview().then(
      res => { if (live) setOverview(res) },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  if (error) return <p className="psw-error" role="alert">{error}</p>
  if (!overview) return <p className="psw-note" role="status">Loading the stock summary</p>

  const awaiting = overview.purchase_orders.awaiting_approval
  return (
    <ul className="psw-tiles" aria-label="Stock summary">
      <li>
        <button type="button" onClick={() => onSection('reorder')}
          className={`psw-tile${overview.below_reorder_level ? ' psw-tile--warn' : ''}`}>
          <span className="psw-tile__value">{overview.below_reorder_level}</span>
          <span className="psw-tile__label">At or below reorder level</span>
        </button>
      </li>
      <li>
        <button type="button" onClick={() => onSection('expiry')}
          className={`psw-tile${overview.near_expiry_lots ? ' psw-tile--warn' : ''}`}>
          <span className="psw-tile__value">{overview.near_expiry_lots}</span>
          <span className="psw-tile__label">
            {`Lots expiring within ${overview.near_expiry_days} days`}
          </span>
        </button>
      </li>
      <li>
        <button type="button" onClick={() => onSection('expiry')}
          className={`psw-tile${overview.expired_on_shelf_units ? ' psw-tile--danger' : ''}`}>
          <span className="psw-tile__value">{overview.expired_on_shelf_units}</span>
          <span className="psw-tile__label">Expired units still on a shelf</span>
        </button>
      </li>
      <li>
        <div className="psw-tile">
          <span className="psw-tile__value">{overview.quarantined_units}</span>
          <span className="psw-tile__label">Units held in quarantine</span>
        </div>
      </li>
      <li>
        <div className="psw-tile">
          <span className="psw-tile__value">{overview.in_transit_units}</span>
          <span className="psw-tile__label">Units on their way between stores</span>
        </div>
      </li>
      <li>
        <button type="button" onClick={() => openView('purchasing')}
          className={`psw-tile${awaiting ? ' psw-tile--warn' : ''}`}>
          <span className="psw-tile__value">{awaiting}</span>
          <span className="psw-tile__label">Orders awaiting approval</span>
        </button>
      </li>
      {overview.without_reorder_level > 0 && (
        <li>
          <button type="button" onClick={() => onSection('reorder')} className="psw-tile">
            <span className="psw-tile__value">{overview.without_reorder_level}</span>
            <span className="psw-tile__label">Drugs with no reorder level set</span>
          </button>
        </li>
      )}
      {overview.legacy_lots > 0 && (
        <li>
          <div className="psw-tile psw-tile--warn">
            <span className="psw-tile__value">{overview.legacy_lots}</span>
            <span className="psw-tile__label">Lots not yet in a store</span>
          </div>
        </li>
      )}
    </ul>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Stock per store and drug
// ═════════════════════════════════════════════════════════════════════════════

function rowKey(r: StockRow): string {
  return `${r.store_id ?? 'none'}:${r.drug_id}`
}

function StockSection({ fail, onLedger }: { fail: Fail; onLedger: (f: LedgerFilter) => void }) {
  const [rows, setRows] = useState<StockRow[] | null>(null)
  const [search, setSearch] = useState('')
  const [error, setError] = useState('')
  const [openKey, setOpenKey] = useState<string | null>(null)

  const load = useCallback((term = '') => {
    getStock({ search: term || undefined }).then(
      res => { setError(''); setRows(res.rows) },
      err => setError(fail(err)),
    )
  }, [fail])

  useEffect(() => { load() }, [load])

  function onSearch(e: FormEvent) {
    e.preventDefault()
    load(search.trim())
  }

  const open = rows?.find(r => rowKey(r) === openKey) ?? null

  return (
    <section className="psw-card" aria-labelledby="psw-stock-title">
      <div className="psw-card__head">
        <h2 id="psw-stock-title" className="psw-card__title">Stock by store</h2>
        <form className="psw-toolbar" onSubmit={onSearch} role="search">
          <label htmlFor="psw-stock-search" className="psw-visually-hidden">Find a drug</label>
          <input id="psw-stock-search" value={search} placeholder="Find a drug by name"
            onChange={e => setSearch(e.target.value)} />
          <button type="submit" className="psw-btn">Find</button>
        </form>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading stock</p>}
      {rows && rows.length === 0 && <p className="psw-note">No stock matches.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Store</th>
                <th scope="col" className="psw-num">Dispensable</th>
                <th scope="col" className="psw-num">Usable</th>
                <th scope="col" className="psw-num">In transit</th>
                <th scope="col" className="psw-num">Quarantined</th>
                <th scope="col" className="psw-num">Expired</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => {
                const key = rowKey(r)
                return (
                  <tr key={key} className={key === openKey ? 'psw-row--open' : undefined}>
                    <td>
                      <span className="psw-strong">{r.drug_name}</span>
                      <span className="psw-sub">{drugLine(r)}</span>
                    </td>
                    <td>
                      <span className="psw-strong">{r.store_label}</span>
                      {r.store_kind_label && <span className="psw-sub">{r.store_kind_label}</span>}
                    </td>
                    <td className="psw-num">{r.dispensable_units}</td>
                    <td className="psw-num">{r.usable_units}</td>
                    <td className="psw-num">{r.in_transit_units}</td>
                    <td className="psw-num">{r.quarantined_units}</td>
                    <td className="psw-num">
                      {r.expired_units > 0
                        ? <span className="psw-chip psw-chip--danger">{r.expired_units}</span>
                        : 0}
                    </td>
                    <td className="psw-actions-cell">
                      <button type="button" className="psw-btn psw-btn--quiet"
                        aria-expanded={key === openKey}
                        onClick={() => setOpenKey(key === openKey ? null : key)}>
                        {key === openKey ? 'Hide lots' : 'Lots'}
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      {open && <LotsPanel key={rowKey(open)} row={open} fail={fail} onLedger={onLedger} />}
    </section>
  )
}

function expiryNote(lot: StockLot): string {
  if (lot.expiry_state === 'undated') return 'No expiry recorded'
  if (lot.expiry_state === 'expired') return 'Expired'
  if (lot.expiry_state === 'near_expiry') return 'Expiring soon'
  return ''
}

function LotsPanel({ row, fail, onLedger }: {
  row: StockRow
  fail: Fail
  onLedger: (f: LedgerFilter) => void
}) {
  const [lots, setLots] = useState<StockLot[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    listStockLots({ drug_id: row.drug_id, store_id: row.store_id }).then(
      res => {
        if (!live) return
        // A lot not yet in any store cannot be asked for by store; keep only
        // the ones in this row's place.
        setLots(res.lots.filter(l => l.store_id === row.store_id))
      },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [row.drug_id, row.store_id, fail])

  return (
    <div className="psw-detail" aria-labelledby="psw-lots-title">
      <div className="psw-card__head">
        <h3 id="psw-lots-title" className="psw-card__title">
          {`Lots of ${row.drug_name} in ${row.store_label}`}
        </h3>
        <button type="button" className="psw-btn psw-btn--quiet"
          onClick={() => onLedger({ drugId: row.drug_id, label: `Movements of ${row.drug_name}` })}>
          Ledger for this drug
        </button>
      </div>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {lots === null && !error && <p className="psw-note" role="status">Loading lots</p>}
      {lots && lots.length === 0 && <p className="psw-note">No lots hold units here.</p>}
      {lots && lots.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Batch</th>
                <th scope="col">Expires</th>
                <th scope="col">Status</th>
                <th scope="col" className="psw-num">Units</th>
                <th scope="col" className="psw-num">Value at cost</th>
                <th scope="col" className="psw-num">Value at MRP</th>
                <th scope="col"><span className="psw-visually-hidden">Actions</span></th>
              </tr>
            </thead>
            <tbody>
              {lots.map(lot => (
                <tr key={lot.id}>
                  <td>{lot.lot_number}</td>
                  <td>
                    <span className="psw-strong">
                      {lot.expiration_date ? formatClinicalDateKey(lot.expiration_date) : 'Not recorded'}
                    </span>
                    {expiryNote(lot) && (
                      <span className={`psw-sub ${lot.expiry_state === 'near_expiry' ? 'psw-chip--warn' : 'psw-chip--danger'}`}>
                        {expiryNote(lot)}
                      </span>
                    )}
                  </td>
                  <td>
                    {lot.status_label}
                    {lot.dispensable && <span className="psw-sub">Dispensable</span>}
                  </td>
                  <td className="psw-num">{lot.quantity}</td>
                  <td className="psw-num">
                    {lot.cost_value_paise === null ? lot.cost_label : formatInr(lot.cost_value_paise)}
                  </td>
                  <td className="psw-num">
                    {lot.mrp_value_paise === null ? lot.mrp_label : formatInr(lot.mrp_value_paise)}
                  </td>
                  <td className="psw-actions-cell">
                    <button type="button" className="psw-btn psw-btn--quiet"
                      onClick={() => onLedger({ lotId: lot.id, label: `Movements of batch ${lot.lot_number}` })}>
                      Ledger
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Reorder
// ═════════════════════════════════════════════════════════════════════════════

function ReorderSection({ fail, openView }: { fail: Fail; openView: (id: string) => void }) {
  const [rows, setRows] = useState<ReorderRow[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    getReorderReport({ include_unset: true }).then(
      res => { if (live) { setError(''); setRows(res.rows) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const due = rows?.filter(r => r.needs_reorder).length ?? 0

  return (
    <section className="psw-card" aria-labelledby="psw-reorder-title">
      <div className="psw-card__head">
        <h2 id="psw-reorder-title" className="psw-card__title">Reorder</h2>
        {due > 0 && (
          <button type="button" className="psw-btn" onClick={() => openView('purchasing')}>
            Draft an order in Purchasing
          </button>
        )}
      </div>
      <p className="psw-hint">
        Each store against its own par level, or the drug&apos;s reorder level on the
        dispensing shelf. Only usable units count; expired and quarantined units do not.
      </p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {rows === null && !error && <p className="psw-note" role="status">Loading reorder levels</p>}
      {rows && rows.length === 0 && <p className="psw-note">No active drugs to show.</p>}
      {rows && rows.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">Drug</th>
                <th scope="col">Store</th>
                <th scope="col">Level</th>
                <th scope="col" className="psw-num">Usable</th>
                <th scope="col" className="psw-num">In transit</th>
                <th scope="col" className="psw-num">On order</th>
                <th scope="col">Suggested</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(r => (
                <tr key={`${r.basis}:${r.store_id ?? 'none'}:${r.drug_id}`}>
                  <td>
                    <span className="psw-strong">{r.drug_name}</span>
                    {r.needs_reorder && <span className="psw-chip psw-chip--warn">Reorder now</span>}
                  </td>
                  <td>{r.store_label}</td>
                  <td>
                    <span className="psw-strong">{r.level_label}</span>
                    {r.basis !== 'none' && <span className="psw-sub">{r.basis_label}</span>}
                  </td>
                  <td className="psw-num">{r.stock_units}</td>
                  <td className="psw-num">{r.in_transit_units}</td>
                  <td className="psw-num">{r.on_order_units}</td>
                  <td>
                    {r.suggested_units !== null
                      ? `${r.suggested_units} units${r.suggested_packs !== null ? `, ${r.suggested_packs} packs` : ''}`
                      : (r.needs_reorder ? (r.note ?? 'Quantity not set') : '')}
                    {r.note && r.suggested_units !== null && <span className="psw-sub">{r.note}</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Near expiry
// ═════════════════════════════════════════════════════════════════════════════

function money(value: number | null, label: string | null): string {
  return value === null ? (label ?? 'Not recorded') : formatInr(value)
}

function ExpirySection({ fail, defaultDays }: { fail: Fail; defaultDays: number | null }) {
  const [report, setReport] = useState<NearExpiryReport | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    getNearExpiry().then(
      res => { if (live) { setError(''); setReport(res) } },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail])

  const days = report?.days ?? defaultDays
  const totals = report?.totals.near_expiry

  return (
    <section className="psw-card" aria-labelledby="psw-expiry-title">
      <h2 id="psw-expiry-title" className="psw-card__title">
        {days ? `Expiring within ${days} days` : 'Near expiry'}
      </h2>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {!report && !error && <p className="psw-note" role="status">Loading expiry dates</p>}
      {report && totals && (
        <p className="psw-note">
          {`${totals.lots} lots, ${totals.units} units. At cost ${formatInr(totals.cost_value_paise)}`}
          {totals.cost_unpriced_lots > 0 ? ` (${totals.cost_unpriced_lots} lots with no recorded cost)` : ''}
          {`. At MRP ${formatInr(totals.mrp_value_paise)}`}
          {totals.mrp_unpriced_lots > 0 ? ` (${totals.mrp_unpriced_lots} lots with no MRP)` : ''}
          .
        </p>
      )}
      {report && report.lots.length === 0 && (
        <p className="psw-note">Nothing on a shelf expires in this window.</p>
      )}
      {report && report.lots.length > 0 && <ExpiryTable lots={report.lots} caption="Expiring soon" />}
      {report && report.expired.length > 0 && (
        <>
          <h3 className="psw-card__title">Expired or undated, still on a shelf</h3>
          <p className="psw-hint">
            These cannot be dispensed. Take them off the shelf and quarantine or write them off.
          </p>
          <ExpiryTable lots={report.expired} caption="Expired or undated" />
        </>
      )}
    </section>
  )
}

function ExpiryTable({ lots, caption }: { lots: StockLot[]; caption: string }) {
  return (
    <div className="psw-table-scroll">
      <table className="psw-table">
        <caption className="psw-visually-hidden">{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Drug</th>
            <th scope="col">Batch</th>
            <th scope="col">Store</th>
            <th scope="col">Expires</th>
            <th scope="col" className="psw-num">Units</th>
            <th scope="col" className="psw-num">Value at cost</th>
            <th scope="col" className="psw-num">Value at MRP</th>
          </tr>
        </thead>
        <tbody>
          {lots.map(lot => (
            <tr key={lot.id}>
              <td>
                <span className="psw-strong">{lot.drug_name}</span>
                <span className="psw-sub">{drugLine(lot)}</span>
              </td>
              <td>{lot.lot_number}</td>
              <td>{lot.store_label}</td>
              <td>{lot.expiration_date ? formatClinicalDateKey(lot.expiration_date) : 'Not recorded'}</td>
              <td className="psw-num">{lot.quantity}</td>
              <td className="psw-num">{money(lot.cost_value_paise, lot.cost_label)}</td>
              <td className="psw-num">{money(lot.mrp_value_paise, lot.mrp_label)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

// ═════════════════════════════════════════════════════════════════════════════
//  Ledger
// ═════════════════════════════════════════════════════════════════════════════

const LEDGER_PAGE = 50

function LedgerSection({ fail, filter, onClear }: {
  fail: Fail
  filter: LedgerFilter
  onClear: () => void
}) {
  const [movements, setMovements] = useState<LedgerMovement[] | null>(null)
  const [cursor, setCursor] = useState<number | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  // The newest page, whenever the filter changes.
  useEffect(() => {
    let live = true
    getLedgerPage({ drug_id: filter.drugId, lot_id: filter.lotId, limit: LEDGER_PAGE }).then(
      res => {
        if (!live) return
        setError('')
        setMovements(res.movements)
        setCursor(res.next_before_seq)
      },
      err => { if (live) setError(fail(err)) },
    )
    return () => { live = false }
  }, [fail, filter.drugId, filter.lotId])

  // An older page, appended. The cursor is a seq, so movements written since
  // the first page never shift what comes next.
  function showOlder(before: number) {
    setBusy(true)
    getLedgerPage({ drug_id: filter.drugId, lot_id: filter.lotId, before_seq: before,
      limit: LEDGER_PAGE }).then(
      res => {
        setError('')
        setMovements(prev => [...(prev ?? []), ...res.movements])
        setCursor(res.next_before_seq)
      },
      err => setError(fail(err)),
    ).finally(() => setBusy(false))
  }

  return (
    <section className="psw-card" aria-labelledby="psw-ledger-title">
      <div className="psw-card__head">
        <h2 id="psw-ledger-title" className="psw-card__title">{filter.label}</h2>
        {(filter.drugId || filter.lotId) && (
          <button type="button" className="psw-btn psw-btn--quiet" onClick={onClear}>
            Show all movements
          </button>
        )}
      </div>
      <p className="psw-hint">Newest first. Every change to drug stock is one line here.</p>
      {error && <p className="psw-error" role="alert">{error}</p>}
      {movements === null && !error && <p className="psw-note" role="status">Loading the ledger</p>}
      {movements && movements.length === 0 && <p className="psw-note">No movements recorded.</p>}
      {movements && movements.length > 0 && (
        <div className="psw-table-scroll">
          <table className="psw-table">
            <thead>
              <tr>
                <th scope="col">When</th>
                <th scope="col">Drug and batch</th>
                <th scope="col">Store</th>
                <th scope="col">Movement</th>
                <th scope="col" className="psw-num">Change</th>
                <th scope="col" className="psw-num">Balance</th>
                <th scope="col">By</th>
              </tr>
            </thead>
            <tbody>
              {movements.map(m => (
                <tr key={m.id}>
                  <td>{formatClinicalDateTime(m.occurred_at) || 'Not recorded'}</td>
                  <td>
                    <span className="psw-strong">{m.drug_name}</span>
                    <span className="psw-sub">{m.lot_number ? `Batch ${m.lot_number}` : ''}</span>
                  </td>
                  <td>{m.store_label}</td>
                  <td>
                    <span className="psw-strong">{m.movement_label}</span>
                    {m.reference_label && <span className="psw-sub">{m.reference_label}</span>}
                    {m.remarks && <span className="psw-sub">{m.remarks}</span>}
                  </td>
                  <td className="psw-num">{m.quantity_change > 0 ? `+${m.quantity_change}` : m.quantity_change}</td>
                  <td className="psw-num">{m.balance_after_units ?? 'Not recorded'}</td>
                  <td>{m.actor_name ?? m.actor_id ?? 'Not recorded'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {cursor !== null && (
        <button type="button" className="psw-btn" disabled={busy} onClick={() => showOlder(cursor)}>
          Show older movements
        </button>
      )}
    </section>
  )
}
