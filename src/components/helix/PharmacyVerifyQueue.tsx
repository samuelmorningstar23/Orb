/**
 * Orb Hospital OS — Pharmacist Verification Queue
 *
 * The closed loop for AI/agentic medications. Lists meds charted with provenance
 * llm_extracted/agentic that no pharmacist has verified yet (dose often
 * "REVIEW REQUIRED"), surfaces any drug-safety alerts in red, and lets the
 * pharmacist correct the dose/route/frequency then Verify (activate) or Reject
 * (discontinue with a reason).
 *
 * Self-contained: takes no props and fetches its own data.
 */
import { Fragment, useEffect, useState } from 'react';
import styles from './Helix.module.css';
import {
  getPendingVerifications,
  verifyMedication,
  rejectMedication,
  type PendingMedication,
  type SafetyAlert,
  type SafetyScreen,
} from '../../api/pharmacy';
import { formatClinicalDateTime } from '../../lib/datetime';

/** The queue row as the server now sends it: where the patient is, who wrote
 *  the order (the prescriber, or the author of the note Sage read it from) and
 *  when it was charted. Optional so an older server still renders. */
type QueueRow = PendingMedication & {
  ward?: string | null;
  bed?: string | null;
  prescriber?: string | null;
  started_at?: string | null;
  /** The ordering person by name (the prescriber, or the note's author). */
  ordered_by_name?: string | null;
};

/** "Ward 3, bed 12", or whichever half is known. */
function wardBed(row: QueueRow): string {
  const ward = (row.ward ?? '').trim();
  const bed = (row.bed ?? '').trim();
  if (ward && bed) return `${ward}, bed ${bed}`;
  if (bed) return `Bed ${bed}`;
  return ward;
}

/** "Ordered by Dr Mehta, 3 Oct 2026, 10:15 AM", or whichever half is known. */
function orderedLine(row: QueueRow): string {
  const who = (row.ordered_by_name ?? row.prescriber ?? '').trim();
  const when = formatClinicalDateTime(row.started_at ?? null);
  if (who && when) return `Ordered by ${who}, ${when}`;
  if (who) return `Ordered by ${who}`;
  return when ? `Charted ${when}` : '';
}

const ROW_DETAIL_STYLE = { fontSize: 12, fontWeight: 400, lineHeight: 1.35, color: 'var(--text-secondary)', marginTop: 2 };

type EditState = Record<number, { dosage: string; route: string; frequency: string }>;

/** Blank, or still the AI charting placeholder ("REVIEW REQUIRED", route
 *  "Review"). Mirrors the server, which answers 422 for these: leaving a field
 *  empty used to keep the placeholder as the verified dose. */
function isPlaceholder(value: string | null | undefined): boolean {
  const text = (value ?? '').trim();
  return text === '' || text.toUpperCase().includes('REVIEW');
}

const INCOMPLETE_HINT = 'Enter a dose, route and frequency before verifying';
/** Per-med inline acknowledge flow, opened when a verify attempt returns 428
 *  (severe alerts or a failed screen). */
type AckState = Record<number, { reason: string; safety: SafetyScreen | null; message: string }>;
/** Per-med inline reject flow: the reason typed so far, keyed by med id. */
type RejectState = Record<number, string>;

/* Surfaces, inputs and buttons are the Helix classes (Helix.module.css), so the
   disabled pair, the clay primary and the 44px coarse-pointer floor come from
   one place. Inline styles here could not take a media query at all. */

/* Small text, so the -text tokens: the fill tokens are tuned for backgrounds
   and fall short of contrast at 11-12px. */
function severityColor(sev: string): string {
  if (sev === 'severe') return 'var(--status-danger-text)';
  if (sev === 'moderate') return 'var(--status-warn-text)';
  return 'var(--text-muted)';
}

/** Outlined pill flagging screening coverage gaps — same shape as the source
 *  badge but coloured by the status tokens so it reads in both themes. */
function ScreeningBadge({ label, color }: { label: string; color: string }) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        fontSize: 11,
        fontWeight: 700,
        letterSpacing: 0.4,
        textTransform: 'uppercase',
        color,
        background: 'transparent',
        border: `1px solid ${color}`,
        borderRadius: 999,
        whiteSpace: 'nowrap',
      }}
    >
      {label}
    </span>
  );
}

function SafetyAlerts({ alerts }: { alerts: SafetyAlert[] }) {
  if (!alerts || alerts.length === 0) {
    return <span style={{ color: 'var(--status-ok-text)', fontSize: 12 }}>No alerts</span>;
  }
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      {alerts.map((a, i) => (
        <div
          key={i}
          style={{
            fontSize: 12,
            lineHeight: 1.35,
            color: severityColor(a.severity),
            fontWeight: a.severity === 'severe' ? 700 : 500,
          }}
        >
          <span style={{ textTransform: 'uppercase', letterSpacing: 0.4 }}>
            {a.type} · {a.severity}
          </span>
          <br />
          {a.message}
        </div>
      ))}
    </div>
  );
}

/** The full safety cell: coverage badge (failed screen / drug outside the
 *  interaction KB / other drugs on the chart outside it) + alert list. A gap
 *  must never render as a green "No alerts". */
function SafetyCell({ row }: { row: PendingMedication }) {
  const failed = row.screen_failed;
  const unscreened = !failed && row.interaction_screened === false;
  // The drug itself was screened but some of the chart was not (the note names
  // those drugs): a clean result only covers the pairs that were checked.
  const partial = !failed && !unscreened && !!row.coverage_note;
  // flex-start: a stretched column drew the coverage pill as a full-width bar.
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 6 }}>
      {failed && (
        <ScreeningBadge label="Screening unavailable" color="var(--status-danger-text)" />
      )}
      {unscreened && <ScreeningBadge label="Not screened" color="var(--status-warn-text)" />}
      {partial && <ScreeningBadge label="Partly screened" color="var(--status-warn-text)" />}
      {(failed || unscreened || partial) && row.coverage_note && (
        <div style={{ fontSize: 11, lineHeight: 1.35, color: 'var(--text-muted)' }}>
          {row.coverage_note}
        </div>
      )}
      {/* Never render the green "No alerts" for a gap: with a failed screen we
          show nothing but the badge, and an unscreened drug only lists the
          allergy alerts it did get. */}
      {!failed && (row.safety_alerts.length > 0 || !(unscreened || partial)) && (
        <SafetyAlerts alerts={row.safety_alerts} />
      )}
    </div>
  );
}

function SourceBadge({ source }: { source: string }) {
  return (
    <span
      style={{
        display: 'inline-block',
        padding: '2px 8px',
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: 0.3,
        textTransform: 'uppercase',
        color: 'var(--text-primary)',
        background: 'color-mix(in srgb, var(--status-info) 10%, transparent)',
        border: '1px solid var(--border-glass)',
        borderRadius: 999,
      }}
    >
      {source === 'llm_extracted' ? 'Sage, from notes' : source === 'agentic' ? 'Sage action' : source}
    </span>
  );
}

export default function PharmacyVerifyQueue() {
  const [rows, setRows] = useState<QueueRow[]>([]);
  const [edits, setEdits] = useState<EditState>({});
  const [acks, setAcks] = useState<AckState>({});
  const [rejects, setRejects] = useState<RejectState>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const pending = await getPendingVerifications();
      setRows(pending);
      // Seed editable fields; blank out the "REVIEW REQUIRED" placeholders so the
      // pharmacist types a real value rather than editing a placeholder.
      const seeded: EditState = {};
      for (const r of pending) {
        const clean = (v: string) => (isPlaceholder(v) ? '' : v.trim());
        seeded[r.id] = { dosage: clean(r.dose), route: clean(r.route), frequency: clean(r.frequency) };
      }
      setEdits(seeded);
    } catch (e) {
      setError((e instanceof Error ? e.message : '') || 'Failed to load the verification queue');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    load();
  }, []);

  function setField(id: number, field: 'dosage' | 'route' | 'frequency', value: string) {
    setEdits((prev) => ({ ...prev, [id]: { ...prev[id], [field]: value } }));
  }

  function closeAck(id: number) {
    setAcks((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  async function onVerify(row: PendingMedication, ackReason?: string) {
    setBusyId(row.id);
    setError(null);
    try {
      const e = edits[row.id] || { dosage: '', route: '', frequency: '' };
      // Always send all three: the buttons are disabled until each is a real
      // value, and the server refuses a blank or placeholder field anyway.
      const result = await verifyMedication(
        row.id,
        { dosage: e.dosage.trim(), route: e.route.trim(), frequency: e.frequency.trim() },
        ackReason,
      );
      if ('code' in result) {
        // 428: severe alerts (or a failed screen) — open the inline acknowledge
        // flow so the pharmacist supplies a reason and re-submits.
        setAcks((prev) => ({
          ...prev,
          [row.id]: {
            reason: prev[row.id]?.reason ?? '',
            safety: result.safety,
            message: result.message,
          },
        }));
        return;
      }
      setRows((prev) => prev.filter((r) => r.id !== row.id));
      closeAck(row.id);
      // Who checked it, by name, as the server recorded it.
      const checker = (result.medication?.verified_by_name ?? '').trim();
      setFlash(`Verified ${row.drug_name} for ${row.patient ?? 'patient'}`
        + (checker ? `. Checked by ${checker}.` : ''));
    } catch (e) {
      setError((e instanceof Error ? e.message : '') || 'Verification failed');
    } finally {
      setBusyId(null);
    }
  }

  function openReject(id: number) {
    setRejects((prev) => (id in prev ? prev : { ...prev, [id]: '' }));
    // Straight to the reason box, which is what Reject now asks for.
    requestAnimationFrame(() => document.getElementById(`reject-reason-${id}-text`)?.focus());
  }

  function closeReject(id: number) {
    setRejects((prev) => {
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }

  async function onReject(row: PendingMedication) {
    const reason = (rejects[row.id] ?? '').trim();
    if (reason === '') return;
    setBusyId(row.id);
    setError(null);
    try {
      await rejectMedication(row.id, reason);
      setRows((prev) => prev.filter((r) => r.id !== row.id));
      closeReject(row.id);
      setFlash(`Rejected ${row.drug_name} for ${row.patient ?? 'patient'}`);
    } catch (e) {
      setError((e instanceof Error ? e.message : '') || 'Rejection failed');
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className={styles.verifyPage}>
      <header className="orb-home__header">
        <div>
          <h2 className="orb-home__title">Pharmacist Verification Queue</h2>
          <p className="orb-home__context">
            Medicines Sage charted, waiting for a pharmacist to check them before they become active orders.
          </p>
        </div>
        <button
          type="button"
          className="orb-home__refresh"
          onClick={load}
          disabled={loading}
          aria-busy={loading || undefined}
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      {flash && <div className={styles.verifyFlash} role="status">{flash}</div>}
      {error && <div className={styles.verifyError} role="alert">{error}</div>}

      {loading ? (
        <div className={styles.verifyCard} style={{ padding: 40, textAlign: 'center', color: 'var(--text-muted)' }}>
          Loading verification queue…
        </div>
      ) : rows.length === 0 ? (
        <div className={styles.verifyCard} style={{ padding: 48, textAlign: 'center' }}>
          <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--status-ok-text)' }}>Queue clear</div>
          <div style={{ marginTop: 6, fontSize: 13, color: 'var(--text-muted)' }}>
            No medicines from Sage are waiting to be checked.
          </div>
        </div>
      ) : (
        <div className={styles.verifyCard} style={{ overflowX: 'auto' }}>
          <table className={styles.verifyTable}>
            <thead>
              <tr>
                <th>Patient</th>
                <th>Drug</th>
                <th>Dose</th>
                <th>Route</th>
                <th>Frequency</th>
                <th>Source</th>
                <th>Safety alerts</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const e = edits[row.id] || { dosage: '', route: '', frequency: '' };
                const busy = busyId === row.id;
                const ack = acks[row.id];
                const rejectReason = rejects[row.id];
                const rejecting = rejectReason !== undefined;
                const incomplete =
                  isPlaceholder(e.dosage) || isPlaceholder(e.route) || isPlaceholder(e.frequency);
                const hintId = `verify-hint-${row.id}`;
                const rejectId = `reject-reason-${row.id}`;
                return (
                  <Fragment key={row.id}>
                    <tr>
                      <td data-label="Patient" className={styles.verifyPatient}>
                        {row.patient ?? 'Not recorded'}
                        {wardBed(row) && <div style={ROW_DETAIL_STYLE}>{wardBed(row)}</div>}
                      </td>
                      <td data-label="Drug">
                        <div>{row.drug_name}</div>
                        {orderedLine(row) && <div style={ROW_DETAIL_STYLE}>{orderedLine(row)}</div>}
                      </td>
                      <td data-label="Dose">
                        <input
                          className={styles.verifyInput}
                          value={e.dosage}
                          aria-label={`Dose for ${row.drug_name}`}
                          placeholder="e.g. 500mg"
                          onChange={(ev) => setField(row.id, 'dosage', ev.target.value)}
                        />
                      </td>
                      <td data-label="Route">
                        <input
                          className={styles.verifyInput}
                          value={e.route}
                          aria-label={`Route for ${row.drug_name}`}
                          placeholder="e.g. Oral"
                          onChange={(ev) => setField(row.id, 'route', ev.target.value)}
                        />
                      </td>
                      <td data-label="Frequency">
                        <input
                          className={styles.verifyInput}
                          value={e.frequency}
                          aria-label={`Frequency for ${row.drug_name}`}
                          placeholder="e.g. TDS"
                          onChange={(ev) => setField(row.id, 'frequency', ev.target.value)}
                        />
                      </td>
                      <td data-label="Source">
                        <SourceBadge source={row.source} />
                      </td>
                      <td data-label="Safety alerts" className={styles.verifySafety}>
                        <SafetyCell row={row} />
                      </td>
                      <td data-label="Actions">
                        <div className={styles.verifyActions}>
                          <button
                            type="button"
                            className={styles.btnPrimary}
                            disabled={busy || incomplete}
                            onClick={() => onVerify(row)}
                            aria-describedby={incomplete ? hintId : undefined}
                          >
                            {busy ? '…' : 'Verify'}
                          </button>
                          <button
                            type="button"
                            className={styles.btnDanger}
                            disabled={busy}
                            onClick={() => openReject(row.id)}
                            aria-expanded={rejecting}
                            aria-controls={rejecting ? rejectId : undefined}
                          >
                            Reject
                          </button>
                        </div>
                        {incomplete && (
                          <div id={hintId} className={styles.verifyHint}>
                            {INCOMPLETE_HINT}
                          </div>
                        )}
                      </td>
                    </tr>
                    {rejecting && (
                      <tr>
                        <td colSpan={8} style={{ padding: '14px 16px' }}>
                          <div id={rejectId} className={styles.reasonPanel} role="group" aria-label={`Reject ${row.drug_name}`}>
                            <label className={styles.reasonTitle} htmlFor={`${rejectId}-text`}>
                              Reason for rejecting {row.drug_name} for {row.patient ?? 'this patient'}
                            </label>
                            <div className={styles.reasonNote}>
                              The medicine is stopped on the chart and the reason is recorded with your name.
                            </div>
                            <textarea
                              id={`${rejectId}-text`}
                              className={styles.verifyInput}
                              style={{ minHeight: 56, resize: 'vertical' }}
                              value={rejectReason}
                              placeholder="Clinical reason (required)"
                              onChange={(ev) =>
                                setRejects((prev) => ({ ...prev, [row.id]: ev.target.value }))
                              }
                            />
                            <div className={styles.verifyActions}>
                              <button
                                type="button"
                                className={styles.btnDanger}
                                disabled={busy || rejectReason.trim() === ''}
                                onClick={() => onReject(row)}
                              >
                                {busy ? '…' : 'Confirm rejection'}
                              </button>
                              <button
                                type="button"
                                className={styles.btnSecondary}
                                disabled={busy}
                                onClick={() => closeReject(row.id)}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                    {ack && (
                      <tr>
                        <td colSpan={8} style={{ padding: '14px 16px' }}>
                          <div className={styles.reasonPanel}>
                            <div className={styles.reasonTitle}>
                              {ack.safety?.screen_failed
                                ? 'Drug-safety screening unavailable: acknowledgement required'
                                : 'Severe safety alerts: acknowledgement required'}
                            </div>
                            <div className={styles.reasonNote}>
                              {ack.message}
                            </div>
                            {ack.safety?.coverage_note && (
                              <div className={styles.reasonWarn}>
                                {ack.safety.coverage_note}
                              </div>
                            )}
                            {ack.safety && ack.safety.alerts && ack.safety.alerts.length > 0 && (
                              <SafetyAlerts alerts={ack.safety.alerts} />
                            )}
                            <textarea
                              className={styles.verifyInput}
                              style={{ minHeight: 56, resize: 'vertical' }}
                              value={ack.reason}
                              aria-label={`Reason for verifying ${row.drug_name} despite the alerts`}
                              placeholder="Clinical reason for verifying despite the alerts (required, audited)"
                              onChange={(ev) =>
                                setAcks((prev) => ({
                                  ...prev,
                                  [row.id]: { ...prev[row.id], reason: ev.target.value },
                                }))
                              }
                            />
                            <div className={styles.verifyActions}>
                              <button
                                type="button"
                                className={styles.btnDanger}
                                disabled={busy || incomplete || ack.reason.trim() === ''}
                                onClick={() => onVerify(row, ack.reason.trim())}
                              >
                                {busy ? '…' : 'Acknowledge & verify'}
                              </button>
                              <button
                                type="button"
                                className={styles.btnSecondary}
                                disabled={busy}
                                onClick={() => closeAck(row.id)}
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
