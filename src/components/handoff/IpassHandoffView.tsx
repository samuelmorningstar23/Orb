/**
 * Orb Hospital OS — I-PASS Shift-Handoff View
 *
 * Self-contained panel that auto-drafts a structured I-PASS handoff from the
 * last ~12h of a patient's data, renders the 5 I-PASS sections, and lets the
 * clinician save the draft or print it. Depends only on src/api/ipass.ts.
 */
import { useEffect, useState, useCallback, type ReactNode } from 'react';
import {
  getIpassDraft,
  prepareReasonedAssessment,
  saveIpass,
  type IpassDraft,
} from '../../api/ipass';
import ReasonedAssessment from './ReasonedAssessment';
import { ASSESSMENT_RECHECK_MS } from './reasonedAssessmentCopy';
import { refusalMessage } from '../../lib/scopeRefusal';
import { humaniseEnum } from '../../lib/humanise';
import { bloodPressure, vitalTemperature, vitalWhole } from '../../lib/vitals';
import { formatClinicalDateTime } from '../../lib/datetime';

interface Props {
  patientId: string;
  patientName?: string;
  /** Name the browser tab after the patient. Only when this view IS the
   *  page (HandoffPage); embedded in a panel it must not retitle the shell. */
  setPageTitle?: boolean;
}

/* Status tokens, not literals. These were raw Tailwind hexes (#dc2626 /
   #d97706 / #16a34a): fixed values that do not follow the theme, so the
   illness-severity marker on the I-PASS sheet stayed the same mid-weight web
   red on warm ivory and on true black alike — night being the theme built for
   reading under fatigue in a dim ward. They also sat outside the contrast gate
   entirely, because the gate resolves tokens.
   These are the FILL tokens, because this value is only ever used as a chip
   background; its label is --status-on-fill, the token verified against all
   four fills in all three themes. The --status-*-text variants are the ones
   for status used AS text and must not be used as a fill. */
const SEVERITY_COLOR: Record<string, string> = {
  Unstable: 'var(--status-danger)',
  Watcher: 'var(--status-warn)',
  Stable: 'var(--status-ok)',
};

function Section({
  tag,
  title,
  children,
}: {
  tag: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section
      style={{
        border: '1px solid var(--border-subtle)',
        borderRadius: 8,
        padding: '14px 16px',
        marginBottom: 14,
        background: 'var(--bg-glass)',
      }}
    >
      <h3 style={{ margin: '0 0 8px', fontSize: '0.95rem', display: 'flex', gap: 8, alignItems: 'center' }}>
        <span
          style={{
            fontWeight: 700,
            fontSize: '0.75rem',
            padding: '2px 7px',
            borderRadius: 4,
            background: 'color-mix(in srgb, var(--status-info) 12%, transparent)',
            // The -text variant: this is small text on a tint, not a fill.
            color: 'var(--status-info-text)',
          }}
        >
          {tag}
        </span>
        {title}
      </h3>
      <div style={{ fontSize: '0.88rem', lineHeight: 1.6 }}>{children}</div>
    </section>
  );
}

export function IpassHandoffView({ patientId, patientName, setPageTitle = false }: Props) {
  const [draft, setDraft] = useState<IpassDraft | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  /** A failed save reads as a failure, not in the info colour of a success. */
  const [saveFailed, setSaveFailed] = useState(false);

  // Several handoffs are often open side by side, and every tab read "Orb".
  const titleName = draft?.sections?.patient_summary?.name || patientName || null;
  const titleBed = draft?.sections?.patient_summary?.bed || null;
  useEffect(() => {
    if (!setPageTitle) return;
    const previous = document.title;
    document.title = titleName
      ? `Handoff: ${titleName}${titleBed ? ` (${titleBed})` : ''}`
      : 'Handoff';
    return () => { document.title = previous; };
  }, [setPageTitle, titleName, titleBed]);

  /** `quiet` re-reads without touching the loading/error chrome — see the
   *  re-check effect below. A quiet failure is dropped on purpose: it is a
   *  background attempt at an optional extra, and the sheet on screen is
   *  already correct. */
  const load = useCallback(async (quiet = false) => {
    if (!quiet) {
      setLoading(true);
      setError(null);
      setSaveMsg(null);
    }
    try {
      const d = await getIpassDraft(patientId);
      setDraft(d);
    } catch (e) {
      if (!quiet) setError(e instanceof Error ? e.message : 'Failed to draft handoff');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [patientId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    void load();
  }, [load]);

  // ── TIER 3: ASK EARLY, READ LATER ──────────────────────────────────────────
  // The handover sheet CARRIES Sage's assessment (sections.ai_assessment) but
  // nothing writes one unless a surface asks, and the sheet itself never
  // generates — it is a deterministic document and stays one. So the asking is
  // this one fire-and-forget call, made the moment the sheet is opened, which
  // is structurally NOT the moment it is read: a handover is composed minutes
  // ahead of the change-over and the ~46 s is spent against a clinician already
  // reading the rest of the page.
  //
  // ITS FAILURE IS SILENT AND THAT IS DELIBERATE. Preparing needs `sage.query`
  // on top of the sheet's own permission — reading a draft is part of seeing the
  // chart, spending the GPU to write one is using Sage — so a caller who may
  // READ this sheet is not guaranteed to be allowed to START a draft. It can
  // also be refused for load, or because the feature is off. Putting any of
  // that in front of someone taking handover, about an optional second opinion
  // they did not ask for, would be noise about a non-event.
  useEffect(() => {
    void prepareReasonedAssessment(patientId).catch(() => undefined);
  }, [patientId]);

  // Re-read quietly a few times while there is no assessment yet. NOT a poll
  // with a spinner: nothing is on screen until there is something to show, so
  // this promises the clinician nothing and simply pays out if the generation
  // lands while they are still on the page. Once one is carried, it stops.
  const assessment = draft?.sections?.ai_assessment ?? null;
  useEffect(() => {
    if (assessment) return;
    const timers = ASSESSMENT_RECHECK_MS.map(ms => setTimeout(() => { void load(true); }, ms));
    return () => timers.forEach(clearTimeout);
  }, [assessment, patientId, load]);

  const handleSave = useCallback(async () => {
    if (!draft) return;
    setSaving(true);
    setSaveMsg(null);
    setSaveFailed(false);
    try {
      const res = await saveIpass(patientId, draft);
      setSaveMsg(`Handoff saved as draft note #${res.id}.`);
    } catch (e) {
      setSaveFailed(true);
      setSaveMsg(e instanceof Error ? e.message : 'Failed to save handoff');
    } finally {
      setSaving(false);
    }
  }, [draft, patientId]);

  const handlePrint = useCallback(() => {
    window.print();
  }, []);

  if (loading) {
    return <div style={{ padding: 20 }}>Drafting I-PASS handoff…</div>;
  }

  if (error) {
    // A scope refusal is not a failure to report back — it is an answer, and
    // one the server worded ambiguously on purpose. Show that wording as-is
    // rather than "Could not draft handoff: Patient not found".
    const refusal = refusalMessage(error);
    return (
      <div style={{ padding: 20 }}>
        <p style={{ color: 'var(--status-danger-text)' }}>
          {refusal ?? `Could not draft handoff: ${error}`}
        </p>
        <button className="btn-glass" onClick={() => void load()}>Retry</button>
      </div>
    );
  }

  if (!draft) {
    return <div style={{ padding: 20 }}>No handoff data available.</div>;
  }

  const s = draft.sections;
  const severityColor = (s.illness_severity.severity && SEVERITY_COLOR[s.illness_severity.severity]) || 'var(--text-muted)';

  // `ipass-print-layout` is the print reveal root (src/print.css hides every
  // element on the page at print time except the named layouts). Without it
  // the Print button produced a blank page.
  return (
    <div className="ipass-print-layout" style={{ maxWidth: 820, margin: '0 auto', padding: 16 }}>
      <header
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          flexWrap: 'wrap',
          gap: 12,
          marginBottom: 16,
        }}
      >
        <div>
          <h2 style={{ margin: 0, fontSize: '1.2rem' }}>
            I-PASS Shift Handoff
          </h2>
          <p style={{ margin: '4px 0 0', fontSize: '0.8rem', color: 'var(--text-muted)' }}>
            {patientName || s.patient_summary.name || patientId} · auto-drafted from the last{' '}
            {draft.window_hours}h · {formatClinicalDateTime(draft.generated_at)}
          </p>
        </div>
        {/* The shared button primitives (index.css): they were bare browser
            buttons. Wraps so neither action can fall off a phone screen. */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }} className="ipass-actions">
          <button className="btn-primary" onClick={handleSave} disabled={saving}>
            {saving ? 'Saving…' : 'Save handoff'}
          </button>
          <button className="btn-glass" onClick={handlePrint}>Print</button>
        </div>
      </header>

      {saveMsg && (
        <p
          className="ipass-save-msg"
          role={saveFailed ? 'alert' : 'status'}
          style={{
            fontSize: '0.85rem',
            margin: '0 0 12px',
            color: saveFailed ? 'var(--status-danger-text)' : 'var(--status-info-text)',
          }}
        >
          {saveMsg}
        </p>
      )}

      {/* I, Illness Severity. No section at all while scoring is switched
          off on this computer: the server drafts no severity (null). */}
      {s.illness_severity.severity != null && (
      <Section tag="I" title="Illness Severity">
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6 }}>
          <span
            style={{
              fontWeight: 700,
              padding: '3px 10px',
              borderRadius: 4,
              color: 'var(--status-on-fill)',
              background: severityColor,
            }}
          >
            {s.illness_severity.severity}
          </span>
          {/* Scale 2 named beside the score (2026-10-02): "NEWS2 0" over a COPD
              patient's SpO₂ 90% reads as a scoring error unless the sheet says
              90 is this patient's target. Scale 1 renders as before. */}
          {s.illness_severity.news2 != null && (
            <span style={{ fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
              NEWS2 {s.illness_severity.news2} ({humaniseEnum(s.illness_severity.news2_level, 'level not assessed')})
              {s.illness_severity.spo2_scale === 2 && ' · SpO₂ Scale 2 (target 88–92%)'}
            </span>
          )}
        </div>
        {s.illness_severity.drivers.length > 0 && (
          <div style={{ fontSize: '0.82rem' }}>
            Drivers: {s.illness_severity.drivers.join(', ')}
          </div>
        )}
      </Section>
      )}

      {/* P — Patient Summary */}
      <Section tag="P" title="Patient Summary">
        <p style={{ margin: '0 0 6px' }}>
          <strong>{s.patient_summary.name}</strong>
          {s.patient_summary.age != null && ` · ${s.patient_summary.age}y`}
          {s.patient_summary.sex && ` ${s.patient_summary.sex}`}
          {' · '}
          {s.patient_summary.ward || 'ward ?'} bed {s.patient_summary.bed || '?'}
        </p>
        <p style={{ margin: '0 0 6px' }}>
          <strong>Diagnosis:</strong> {s.patient_summary.diagnosis || 'not documented'}
        </p>
        <p style={{ margin: '0 0 6px' }}>
          <strong>Active problems:</strong>{' '}
          {s.patient_summary.problems.length ? s.patient_summary.problems.join(', ') : 'none recorded'}
        </p>
        <p style={{ margin: 0 }}>
          <strong>Allergies:</strong>{' '}
          {s.patient_summary.allergies.length
            ? s.patient_summary.allergies.join(', ')
            : 'No allergies recorded. Confirm with the patient.'}
        </p>
      </Section>

      {/* A — Action List */}
      <Section tag="A" title="Action List">
        <p style={{ margin: '0 0 4px' }}><strong>Active medications</strong></p>
        {s.action_list.active_medications.length ? (
          <ul style={{ margin: '0 0 10px', paddingLeft: 20 }}>
            {s.action_list.active_medications.map((m, i) => (
              <li key={i}>
                {m.drug_name} {m.dosage} {m.route} {m.frequency}
                {/* Not a prescription until a pharmacist has checked it. */}
                {m.unverified && <em> (AI-extracted, awaiting pharmacist verification)</em>}
                {/* Charted, but not being given until the prescriber resumes it. */}
                {m.on_hold && (
                  <em>
                    {' (on hold'}
                    {m.held_at ? ` since ${formatClinicalDateTime(m.held_at)}` : ''}
                    {m.hold_reason ? `: ${m.hold_reason}` : ''}
                    {')'}
                  </em>
                )}
              </li>
            ))}
          </ul>
        ) : (
          <p style={{ margin: '0 0 10px', color: 'var(--text-muted)' }}>None charted.</p>
        )}

        {s.action_list.open_care_bundles.length > 0 && (
          <>
            <p style={{ margin: '0 0 4px' }}><strong>Open care bundles</strong></p>
            <ul style={{ margin: '0 0 10px', paddingLeft: 20 }}>
              {s.action_list.open_care_bundles.map((b, i) => (
                <li key={i}>
                  {b.bundle_label || 'Care bundle'} ({b.compliance_pct ?? '—'}% compliant),{' '}
                  {b.outstanding.length ? `outstanding: ${b.outstanding.join(', ')}` : 'all elements complete'}
                </li>
              ))}
            </ul>
          </>
        )}

        {s.action_list.outstanding_items.length > 0 && (
          <>
            <p style={{ margin: '0 0 4px' }}><strong>Outstanding items</strong></p>
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {s.action_list.outstanding_items.map((item, i) => (
                <li key={i}>{item}</li>
              ))}
            </ul>
          </>
        )}
      </Section>

      {/* S — Situation Awareness & Contingency */}
      <Section tag="S" title="Situation Awareness & Contingency">
        {/* `trend` is the raw enum on the wire and stays that way — anything
            matching on it downstream keeps working. This sheet is read at a
            bedside and printed, so it shows the server's own clinical phrasing
            (`trend_display`, e.g. "not enough data to call a trend") and falls
            back to humanising the enum rather than printing `insufficient_data`
            at a nurse taking handover. */}
        <p style={{ margin: '0 0 6px' }}>
          Trend over shift:{' '}
          <strong>
            {s.situation_awareness.trend_display
              || humaniseEnum(s.situation_awareness.trend, 'not assessed')}
          </strong>{' '}
          ({s.situation_awareness.trend_detail})
          {s.situation_awareness.active_deterioration && (
            <span style={{ color: 'var(--status-danger-text)', fontWeight: 700 }}> · active deterioration</span>
          )}
        </p>
        {/* Whole numbers, because that is how these were measured — see
            lib/vitals. Temperature keeps its tenth. */}
        <p style={{ margin: '0 0 8px', fontSize: '0.82rem', color: 'var(--text-secondary)' }}>
          Latest: HR {vitalWhole(s.situation_awareness.latest_vitals.heart_rate, '--')},{' '}
          BP {bloodPressure(
            s.situation_awareness.latest_vitals.systolic_bp,
            s.situation_awareness.latest_vitals.diastolic_bp,
            '--',
          )},{' '}
          SpO₂ {vitalWhole(s.situation_awareness.latest_vitals.spo2, '--')}%,{' '}
          RR {vitalWhole(s.situation_awareness.latest_vitals.respiratory_rate, '--')},{' '}
          Temp {vitalTemperature(s.situation_awareness.latest_vitals.temperature, '--')}°C
        </p>
        {/* No contingency heading over an empty list: with scoring switched
            off the server drafts no NEWS2 contingency lines. */}
        {s.situation_awareness.contingencies.length > 0 && (
          <>
            <p style={{ margin: '0 0 4px' }}><strong>Contingency planning</strong></p>
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {s.situation_awareness.contingencies.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </>
        )}
        {/* The nurses' shift notes of the window, as written and under their
            names (21 October 2026). The same rule as the contingencies: no
            heading over nothing. */}
        {(s.nursing_notes?.length ?? 0) > 0 && (
          <div data-testid="ipass-nursing-notes">
            <p style={{ margin: '8px 0 4px' }}><strong>Nursing notes (last {draft.window_hours}h)</strong></p>
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {(s.nursing_notes ?? []).map(n => (
                <li key={n.id}>
                  <span style={{ color: 'var(--text-secondary)' }}>
                    {n.written_at_display}, {n.shift_label}, {n.author_name}:
                  </span>{' '}
                  {n.content}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Section>

      {/* THE AI-DRAFTED SUPPLEMENT — Sage's four-section assessment.
          Placed after the five I-PASS sections and BEFORE synthesis, matching
          the order of the printed sheet (`_render_plain_text` puts the
          supplement above "S — SYNTHESIS BY RECEIVER", and
          test_assessment_relocation pins that ordering): the receiver's
          synthesis is the last word on a handover and a machine draft does not
          come after it. Renders nothing at all when there is no assessment. */}
      <ReasonedAssessment assessment={s.ai_assessment} />

      {/* S — Synthesis by Receiver */}
      <Section tag="S" title="Synthesis by Receiver">
        <p style={{ margin: 0 }}>{s.synthesis.text}</p>
      </Section>

      {/* Raw rendered handoff (source of truth for print/copy) */}
      <details className="ipass-plain-text" style={{ marginTop: 8 }}>
        <summary style={{ cursor: 'pointer', fontSize: '0.82rem', color: 'var(--text-muted)' }}>
          Plain-text handoff
        </summary>
        <pre
          style={{
            whiteSpace: 'pre-wrap',
            fontSize: '0.78rem',
            padding: 12,
            border: '1px solid var(--border-subtle)',
            borderRadius: 6,
            marginTop: 8,
          }}
        >
          {draft.plain_text}
        </pre>
      </details>
    </div>
  );
}

export default IpassHandoffView;
