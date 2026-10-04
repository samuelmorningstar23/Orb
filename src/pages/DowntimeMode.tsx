/**
 * Orb Hospital OS — EHR Downtime Mode
 *
 * A clean, high-contrast, print-friendly, READ-ONLY view of a patient's durable
 * local snapshot. Designed to be the ward's safe fallback when the EHR, network,
 * or LLM is unavailable: a paper-like document that shows the must-know clinical
 * picture (allergies front and centre) and prints on one tap.
 *
 * Self-contained: renders entirely from the snapshot the backend already froze
 * into local SQLite. No live data, no streaming, no LLM.
 *
 * OFFLINE, HONESTLY. The snapshot lives on the server, so this page needs Orb
 * to answer. When it cannot (no network, or the appliance is down) it shows the
 * copy this device kept from the last time it opened this patient's snapshot,
 * labelled as a copy with its capture time, through the same device cache and
 * rules as the nurse board (lib/offline/phiCache: this user only, this shift
 * only, wiped on sign-out). With no copy it says to use the printed ward pack,
 * never a raw "Failed to fetch".
 */
import { useCallback, useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import {
  captureSnapshot,
  fetchWardPackHtml,
  getLatestSnapshot,
  type DowntimeSnapshotEnvelope,
} from '../api/downtime';
import { refusalMessage } from '../lib/scopeRefusal';
import { bloodPressure, vitalTemperature, vitalWhole } from '../lib/vitals';
import { formatClinicalDate, formatClinicalDateTime } from '../lib/datetime';
import { useOfflineSnapshot } from '../hooks/useOfflineSnapshot';
import OfflineBanner from '../components/OfflineBanner';
import './DowntimeMode.css';

interface DowntimeModeProps {
  /** Optional explicit patient id. Falls back to the :patientId route param. */
  patientId?: string;
}

function fmt(dt?: string | null): string {
  if (!dt) return '—';
  // A bare date (admission_date) is a calendar day, not an instant.
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(dt);
  if (day) return formatClinicalDate(new Date(+day[1], +day[2] - 1, +day[3]));
  // Bare DB timestamps are UTC; formatClinicalDateTime reads them that way.
  // new Date(raw) read them as local time and shifted every printed time.
  return formatClinicalDateTime(dt) || String(dt);
}

/** Said instead of a raw "Failed to fetch" whenever Orb does not answer. */
const UNREACHABLE_GUIDANCE =
  'Orb cannot be reached. Use the printed ward pack kept on the ward until it is back.';

/** No answer at all (fetch rejects with a TypeError) or a server-side failure:
 *  the cases where the device copy is the right fallback. A refusal (403, 404)
 *  is an answer, and a cached copy must never stand in for one. */
function isUnreachable(e: unknown): boolean {
  const status = (e as { status?: unknown } | null)?.status;
  if (typeof status === 'number') return status >= 500;
  return e instanceof TypeError;
}

function levelClass(level?: string | null): string {
  switch ((level || '').toLowerCase()) {
    case 'critical': return 'dt-badge dt-badge--critical';
    case 'elevated': return 'dt-badge dt-badge--elevated';
    case 'stable': return 'dt-badge dt-badge--stable';
    default: return 'dt-badge';
  }
}

/** Where a drug on the downtime sheet came from, in words, never the raw
 *  column value. An AI-extracted order is never printed like a prescription:
 *  it says whether a pharmacist has verified it, or that this copy cannot say. */
function medSourceLabel(m: { source?: string | null }): string {
  const source = (m.source || '').toLowerCase();
  if (source === 'llm_extracted' || source === 'agentic') {
    if (!('verified_by' in m)) return 'AI-extracted, verification not shown in this copy: check the chart';
    return (m as { verified_by?: string | null }).verified_by
      ? 'AI-extracted, verified by a pharmacist'
      : 'AI-extracted, not verified by a pharmacist';
  }
  if (source === 'clinician') return 'Clinician order';
  if (source === 'ingested_fhir') return 'Imported record';
  return m.source ? 'Other source' : '—';
}

/** admissions.code_status as the admission form offers it; the server's ward
 *  pack prints the same words. Anything else is shown as stored. */
const CODE_STATUS_LABELS: Record<string, string> = {
  full_code: 'Full code',
  dnr: 'DNR (do not resuscitate)',
  dni: 'DNI (do not intubate)',
  comfort: 'Comfort care only',
};

/** A stored key in words: "admitted" reads "Admitted", "new_confusion"
 *  reads "New confusion". The page printed the keys as they were stored. */
function inWords(raw?: string | null): string {
  const words = (raw || '').replace(/_/g, ' ').trim()
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** ACVPU as charted, in words. Single letters are expanded so a printed sheet
 *  does not need the chart's legend beside it. */
const ACVPU_WORDS: Record<string, string> = {
  a: 'Alert', c: 'New confusion', v: 'Responds to voice', p: 'Responds to pain', u: 'Unresponsive',
}

function consciousnessText(raw?: string | null): string {
  const key = (raw || '').trim().toLowerCase()
  return ACVPU_WORDS[key] ?? inWords(raw)
}

/** Who captured the snapshot: the scheduled capture says so, a person is
 *  named. Older copies carry only the user id, which is shown as a fallback. */
function capturedBy(env: { created_by?: string | null; created_by_name?: string | null }): string {
  const who = (env.created_by || '').trim()
  if (!who || who.toLowerCase() === 'auto') return 'captured automatically'
  return `captured by ${env.created_by_name || who}`
}

/** Said for a section an older snapshot (schema 1) did not capture. */
const NOT_IN_COPY = 'Not included in this copy. Check the chart.';

function codeStatusText(snap: { code_status?: string | null }): string {
  if (!('code_status' in snap)) return NOT_IN_COPY;
  const raw = (snap.code_status || '').trim();
  if (!raw) return 'Not recorded';
  return CODE_STATUS_LABELS[raw.toLowerCase()] ?? raw.replace(/_/g, ' ');
}

/** A lab value as reported: the number when there is one, else the text. */
function labValue(r: { value_numeric?: number | null; value_text?: string | null; unit?: string | null }): string {
  const v = r.value_numeric ?? r.value_text;
  if (v == null || v === '') return 'Not recorded';
  return r.unit ? `${v} ${r.unit}` : String(v);
}

export default function DowntimeMode({ patientId: patientIdProp }: DowntimeModeProps) {
  const params = useParams<{ patientId?: string }>();
  const patientId = patientIdProp || params.patientId || '';

  const [env, setEnv] = useState<DowntimeSnapshotEnvelope | null>(null);
  const [loading, setLoading] = useState(true);
  const [capturing, setCapturing] = useState(false);
  const [openingPack, setOpeningPack] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** The last read got no answer from Orb (as opposed to a refusal). */
  const [liveFailed, setLiveFailed] = useState(false);

  const load = useCallback(async () => {
    if (!patientId) {
      setError('No patient selected.');
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const latest = await getLatestSnapshot(patientId);
      setEnv(latest);
      setLiveFailed(false);
    } catch (e) {
      setEnv(null);
      const message = e instanceof Error ? e.message : undefined;
      // 404 = no snapshot captured yet; treat as an empty-but-valid state.
      if (message && /no snapshot/i.test(message)) {
        setLiveFailed(false);
        setError(null);
      } else if (isUnreachable(e)) {
        // Not an error box: the page falls back to this device's copy, or to
        // the guidance below when there is none.
        setLiveFailed(true);
      } else {
        setLiveFailed(false);
        setError(refusalMessage(message) || message || 'Could not load the snapshot.');
      }
    } finally {
      setLoading(false);
    }
  }, [patientId]);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
  useEffect(() => { void load(); }, [load]);

  const handleCapture = async () => {
    if (!patientId) return;
    setCapturing(true);
    setError(null);
    try {
      const result = await captureSnapshot(patientId);
      setEnv(result);
      setLiveFailed(false);
    } catch (e) {
      const message = e instanceof Error ? e.message : undefined;
      setError(
        isUnreachable(e)
          ? `Could not capture a new snapshot. ${UNREACHABLE_GUIDANCE}`
          : refusalMessage(message) || message || 'Capture failed.',
      );
    } finally {
      setCapturing(false);
    }
  };

  const handleWardPack = async () => {
    if (!patientId) return;
    setOpeningPack(true);
    setError(null);
    try {
      const html = await fetchWardPackHtml(patientId);
      // Open as a blob URL so the tab works with either cookie or token auth,
      // then print from the browser in that tab.
      const url = URL.createObjectURL(new Blob([html], { type: 'text/html' }));
      window.open(url, '_blank', 'noopener');
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) {
      const message = e instanceof Error ? e.message : undefined;
      setError(
        isUnreachable(e)
          ? `The ward pack could not be opened. ${UNREACHABLE_GUIDANCE}`
          : refusalMessage(message) || message || 'Could not open the ward pack.',
      );
    } finally {
      setOpeningPack(false);
    }
  };

  // Live when Orb answered; this device's copy (labelled) when it did not.
  const offline = useOfflineSnapshot<DowntimeSnapshotEnvelope>(
    `downtime:${patientId}`,
    loading ? null : env,
    { liveFailed, onReconnect: () => void load() },
  );
  const shown = offline.data;
  const isCopy = offline.source === 'cache';
  const degraded = isCopy || !offline.online || offline.unreachable;
  const snap = shown?.snapshot;
  const demo = snap?.demographics;
  // Which SpO₂ table the NEWS2 was scored on (COPD / type-2 failure = Scale 2).
  const scale2 = snap?.news2?.spo2_scale === 2;

  // Name the tab: several of these open side by side during an outage, and
  // every one of them used to read "Orb".
  const titleName = demo?.name || null;
  const titleBed = demo?.bed || null;
  useEffect(() => {
    const previous = document.title;
    document.title = titleName
      ? `Downtime: ${titleName}${titleBed ? ` (${titleBed})` : ''}`
      : 'Downtime';
    return () => { document.title = previous; };
  }, [titleName, titleBed]);

  const escalations = snap?.open_escalations;
  const documents = snap?.recent_documents;
  const labResults = snap?.lab_results;
  const labReports = snap?.recent_lab_reports;
  const labsInCopy = !!snap && ('lab_results' in snap || 'recent_lab_reports' in snap);

  return (
    <div className="downtime-mode">
      {/* Toolbar (hidden when printing) */}
      <div className="dt-toolbar">
        <div className="dt-toolbar__title">
          <span className="dt-dot" aria-hidden /> EHR Downtime Mode
          <span className="dt-toolbar__sub">
            Read-only snapshot · readable offline only if this device opened it this shift
          </span>
        </div>
        <div className="dt-toolbar__actions">
          <button className="dt-btn" onClick={() => void load()} disabled={loading || capturing}>
            Reload
          </button>
          <button className="dt-btn dt-btn--primary" onClick={handleCapture} disabled={capturing || !patientId}>
            {capturing ? 'Capturing…' : env ? 'Re-capture now' : 'Capture snapshot'}
          </button>
          <button className="dt-btn" onClick={() => window.print()} disabled={!shown}>
            Print
          </button>
          <button className="dt-btn" onClick={() => void handleWardPack()} disabled={openingPack || !patientId}>
            {openingPack ? 'Opening…' : 'Print ward pack'}
          </button>
        </div>
      </div>

      {degraded && <OfflineBanner snapshot={offline} what="downtime snapshot" onRetry={() => void load()} />}

      {error && <div className="dt-alert" role="alert">{error}</div>}

      {/* Orb did not answer and this device holds no copy of this patient. */}
      {!loading && degraded && !shown && (
        <div className="dt-alert" role="alert">{UNREACHABLE_GUIDANCE}</div>
      )}

      {loading && <div className="dt-empty">Loading snapshot…</div>}

      {!loading && !shown && !error && !degraded && (
        <div className="dt-empty">
          <p>No downtime snapshot has been captured for this patient yet.</p>
          <p>Capture one now so a safe local copy is available if the EHR goes down.</p>
        </div>
      )}

      {!loading && shown && snap && (
        <div className="downtime-print-layout">
          {/* Banner. A device copy says so on screen and on paper. */}
          <div className="dt-banner">
            <div className="dt-banner__label">{isCopy ? 'OFFLINE COPY OF DOWNTIME SNAPSHOT' : 'DOWNTIME SNAPSHOT'}</div>
            <div className="dt-banner__time">
              {isCopy ? 'Offline copy captured at' : 'Captured at'} {fmt(shown.created_at)}
            </div>
            <div className="dt-banner__note">
              {isCopy
                ? 'Read-only copy kept on this device. Anything charted since that time is not shown.'
                : `Read-only · assembled locally · ${capturedBy(shown)}`}
            </div>
          </div>

          {/* Demographics */}
          <section className="dt-section dt-demographics">
            <h1 className="dt-patient-name">{demo?.name || 'Unknown patient'}</h1>
            <div className="dt-demo-grid">
              <div><span className="dt-k">MRN</span><span className="dt-v">{demo?.mrn || '—'}</span></div>
              <div><span className="dt-k">Age / Sex</span><span className="dt-v">{demo?.age ?? '—'} / {demo?.sex || '—'}</span></div>
              <div><span className="dt-k">Ward / Bed</span><span className="dt-v">{demo?.ward || '—'} / {demo?.bed || '—'}</span></div>
              <div><span className="dt-k">Status</span><span className="dt-v">{demo?.status ? inWords(demo.status) : '—'}</span></div>
              <div><span className="dt-k">Diagnosis</span><span className="dt-v">{demo?.diagnosis || '—'}</span></div>
              <div><span className="dt-k">Admitted</span><span className="dt-v">{fmt(demo?.admission_date)}</span></div>
              <div><span className="dt-k">Attending</span><span className="dt-v">{demo?.attending_doctor_name || '—'}</span></div>
              <div><span className="dt-k">Emergency contact</span><span className="dt-v">{demo?.emergency_contact_name || '—'} {demo?.emergency_contact_phone ? `(${demo.emergency_contact_phone})` : ''}</span></div>
            </div>
          </section>

          {/* Allergies — most safety-critical: prominent */}
          <section className={`dt-section dt-allergies ${snap.allergies.length ? 'dt-allergies--present' : ''}`}>
            <h2 className="dt-section-title">Allergies</h2>
            {snap.allergies.length === 0 ? (
              /* Not "NKDA": an empty list is also a patient nobody asked. */
              <div className="dt-nkda">No allergies recorded. Confirm with the patient.</div>
            ) : (
              <ul className="dt-allergy-list">
                {snap.allergies.map((a, i) => (
                  <li key={i} className="dt-allergy">
                    <span className="dt-allergy__name">{a.substance}</span>
                    {a.severity && <span className="dt-allergy__sev">{a.severity}</span>}
                    {a.reaction && <span className="dt-allergy__rx">({a.reaction})</span>}
                    {a.substance_class && <span className="dt-allergy__cls">({a.substance_class})</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Code status and open escalation: the first two questions at a
              crash call, so they sit right under the allergies. */}
          <section className="dt-section">
            <h2 className="dt-section-title">Code status and escalation</h2>
            <div className="dt-demo-grid">
              <div><span className="dt-k">Code status</span><span className="dt-v dt-v--strong">{codeStatusText(snap)}</span></div>
              <div>
                <span className="dt-k">Escalation</span>
                <span className="dt-v">
                  {escalations === undefined
                    ? NOT_IN_COPY
                    : escalations.length === 0
                      ? 'None open at capture time'
                      : escalations.map((e, i) => (
                        <span key={i} className="dt-escalation">
                          {e.status === 'acknowledged' ? 'Acknowledged' : 'Open'}, tier {e.current_tier ?? '?'} of {e.max_tier ?? '?'}
                          {(e.tier_label || e.tier_role) ? `, ${e.tier_label || e.tier_role}` : ''}
                          {e.raised_at ? `, raised ${fmt(e.raised_at)}` : ''}
                        </span>
                      ))}
                </span>
              </div>
            </div>
          </section>

          {/* Latest vitals + NEWS2 */}
          <section className="dt-section">
            <h2 className="dt-section-title">
              Latest vitals
              {snap.news2?.raw_news2 != null && (
                <span className={levelClass(snap.news2.level)}>
                  NEWS2 {snap.news2.raw_news2} · {snap.news2.level || '—'}
                  {/* 2026-10-02: unmarked, a Scale 2 "NEWS2 0" beside SpO₂ 90% is
                      re-scored as 3 on the default Scale 1 paper chart. */}
                  {scale2 && ' · SpO₂ Scale 2 (target 88–92%)'}
                </span>
              )}
            </h2>
            {snap.latest_vitals ? (
              <>
                {/* This page is printed and taped above a bed. Vitals render at
                    the precision they were measured at (lib/vitals): whole mmHg
                    and whole counts, a tenth only for temperature. */}
                <div className="dt-vitals-grid">
                  <div className="dt-vital"><span className="dt-vital__v">{vitalWhole(snap.latest_vitals.heart_rate)}</span><span className="dt-vital__k">HR (bpm)</span></div>
                  <div className="dt-vital"><span className="dt-vital__v">{bloodPressure(snap.latest_vitals.systolic_bp, snap.latest_vitals.diastolic_bp)}</span><span className="dt-vital__k">BP (mmHg)</span></div>
                  <div className="dt-vital"><span className="dt-vital__v">{vitalWhole(snap.latest_vitals.spo2)}%</span><span className="dt-vital__k">SpO₂{scale2 ? (snap.latest_vitals.on_oxygen ? ' (Scale 2, O₂)' : ' (Scale 2)') : (snap.latest_vitals.on_oxygen ? ' (O₂)' : '')}</span></div>
                  <div className="dt-vital"><span className="dt-vital__v">{vitalWhole(snap.latest_vitals.respiratory_rate)}</span><span className="dt-vital__k">RR (/min)</span></div>
                  <div className="dt-vital"><span className="dt-vital__v">{vitalTemperature(snap.latest_vitals.temperature)}</span><span className="dt-vital__k">Temp (°C)</span></div>
                  <div className="dt-vital"><span className="dt-vital__v">{snap.latest_vitals.consciousness ? consciousnessText(snap.latest_vitals.consciousness) : '—'}</span><span className="dt-vital__k">ACVPU</span></div>
                </div>
                <div className="dt-meta">Recorded {fmt(snap.latest_vitals.timestamp)}</div>
                {snap.news2?.drivers && snap.news2.drivers.length > 0 && (
                  <div className="dt-drivers">Drivers: {snap.news2.drivers.join(' · ')}</div>
                )}
              </>
            ) : (
              <div className="dt-empty-inline">No vitals recorded.</div>
            )}
          </section>

          {/* Problems */}
          <section className="dt-section">
            <h2 className="dt-section-title">Active problems ({snap.problems.length})</h2>
            {snap.problems.length === 0 ? (
              <div className="dt-empty-inline">No active problems recorded.</div>
            ) : (
              <ul className="dt-list">
                {snap.problems.map((p, i) => (
                  <li key={i}>
                    {p.problem}
                    {p.icd10_code && <span className="dt-code"> [{p.icd10_code}]</span>}
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Medications */}
          <section className="dt-section">
            <h2 className="dt-section-title">Active medications ({snap.medications.length})</h2>
            {snap.medications.length === 0 ? (
              <div className="dt-empty-inline">No active medications recorded.</div>
            ) : (
              <div className="dt-table-wrap">
                <table className="dt-table">
                  <thead>
                    <tr><th>Drug</th><th>Dose</th><th>Route</th><th>Frequency</th><th>Source</th></tr>
                  </thead>
                  <tbody>
                    {snap.medications.map((m, i) => (
                      <tr key={i}>
                        <td>{m.drug_name}</td>
                        <td>{m.dosage || '—'}</td>
                        <td>{m.route || '—'}</td>
                        <td>{m.frequency || '—'}</td>
                        <td>{medSourceLabel(m)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Recent lab results: interfaced analytes, then Orb's lab reports */}
          <section className="dt-section">
            <h2 className="dt-section-title">Recent lab results</h2>
            {!labsInCopy ? (
              <div className="dt-empty-inline">{NOT_IN_COPY}</div>
            ) : !labResults?.length && !labReports?.length ? (
              <div className="dt-empty-inline">No recent lab results recorded.</div>
            ) : (
              <>
                {!!labResults?.length && (
                  <div className="dt-table-wrap">
                    <table className="dt-table">
                      <thead>
                        <tr><th>Test</th><th>Result</th><th>Range</th><th>Flag</th><th>Taken</th></tr>
                      </thead>
                      <tbody>
                        {labResults.map((r, i) => (
                          <tr key={i}>
                            <td>{r.observation_name}</td>
                            <td>{labValue(r)}</td>
                            <td>{r.reference_range || ''}</td>
                            <td>{r.abnormal_flag || ''}</td>
                            <td>{fmt(r.observed_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                {!!labReports?.length && (
                  <ul className="dt-notes dt-notes--after-table">
                    {labReports.map((r) => (
                      <li key={r.id} className="dt-note">
                        <div className="dt-note__head">
                          <span className="dt-note__fmt">{(r.title || 'Lab report').toUpperCase()}</span>
                          <span className="dt-note__meta">{fmt(r.created_at)}</span>
                        </div>
                        <div className="dt-note__body">{r.content || 'No text recorded.'}</div>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}
          </section>

          {/* Notes written in the chart (not dictated through Scribe) */}
          <section className="dt-section">
            <h2 className="dt-section-title">
              Recent chart notes{documents ? ` (${documents.length})` : ''}
            </h2>
            {documents === undefined ? (
              <div className="dt-empty-inline">{NOT_IN_COPY}</div>
            ) : documents.length === 0 ? (
              <div className="dt-empty-inline">No recent chart notes.</div>
            ) : (
              <ul className="dt-notes">
                {documents.map((d) => (
                  <li key={d.id} className="dt-note">
                    <div className="dt-note__head">
                      <span className="dt-note__fmt">{(d.title || d.doc_type || 'Note').toUpperCase()}</span>
                      <span className="dt-note__meta">{fmt(d.created_at)}</span>
                    </div>
                    <div className="dt-note__body">{d.content || 'No text recorded.'}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {/* Recent notes */}
          <section className="dt-section">
            <h2 className="dt-section-title">Recent signed notes ({snap.recent_notes.length})</h2>
            {snap.recent_notes.length === 0 ? (
              <div className="dt-empty-inline">No signed notes.</div>
            ) : (
              <ul className="dt-notes">
                {snap.recent_notes.map((n) => (
                  <li key={n.id} className="dt-note">
                    <div className="dt-note__head">
                      <span className="dt-note__fmt">{(n.note_format || 'note').toUpperCase()}</span>
                      <span className="dt-note__meta">
                        {n.signed_by_name || n.author_name || n.signed_by || n.author_id || 'Author not recorded'} · {fmt(n.signed_at || n.created_at)}
                      </span>
                    </div>
                    <div className="dt-note__body">{n.structured_note || '—'}</div>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <div className="dt-footer">
            Orb Hospital OS · Downtime snapshot #{shown.id} · This is a point-in-time local
            copy and may not reflect changes made after {fmt(shown.created_at)}.
          </div>
        </div>
      )}
    </div>
  );
}
