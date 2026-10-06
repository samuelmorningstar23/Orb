import React, { useEffect, useState } from 'react'
import OrbLogo from '../OrbLogo'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import DOMPurify from 'dompurify'
import { describeAnswerProvenance, type SageProvenance } from '../../api/thinkingMode'
import type { SageSourceRef, SageStreamMetadata, SageStreamTiming, SageStructuredResponse, SageVoiceTiming } from '../../api/client'
import Icon, { type IconName } from '../common/Icon'
import { noSourcesNote, sourcesHeading, type SageSource } from './sageSources'

// Which-model notices draw a line icon by kind; the notice's own emoji field
// is not rendered (emoji cannot take the theme colour).
const NOTICE_ICON: Record<'fast' | 'floor' | 'unsatisfied', IconName> = {
  unsatisfied: 'alert',
  floor: 'shield',
  fast: 'zap',
}

// ─── Utilities ───

// ── Fast-contract markers ──
// Sage's fast lane opens every reply with `NOACT|`, or `ACT|{intent json}` when
// it is requesting a chart change. The server strips that syntax out of the
// STREAM, but the turn is persisted verbatim (the raw model output is the
// provenance record), so a conversation reopened from /sage/history rendered
// "NOACT| National Early Warning Score version 2…" straight into the chat
// bubble. Contract syntax is never clinical content: strip it at the render
// layer, which is the one place every lane — streamed, reloaded, replayed —
// has to pass through. Mirrors the server's own marker regexes.
const MARKER_LEAD_RE = /^(\s*(?:```[a-zA-Z]*\s*)?)(NOACT\||ACT\|)/;
// `(?<![A-Z])` keeps this off the tail of a word (and off NOACT|'s own ACT|).
const MARKER_ANYWHERE_RE = /(?:NOACT\||(?<![A-Z])ACT\|)[ \t]?/g;

function stripContractMarkers(rawText: string): string {
  if (!rawText) return '';
  let text = rawText;
  const lead = text.match(MARKER_LEAD_RE);
  if (lead && lead[2] === 'ACT|') {
    // The action intent is the first balanced {…} after the marker; the
    // clinician-visible answer starts after it.
    const rest = text.slice(lead[0].length);
    let depth = 0;
    let close = -1;
    for (let i = 0; i < rest.length; i++) {
      if (rest[i] === '{') depth++;
      else if (rest[i] === '}' && --depth === 0) { close = i + 1; break; }
    }
    text = lead[1] + (close > -1 ? rest.slice(close) : rest);
  }
  // Leading NOACT|, plus any marker the model repeated mid-answer.
  return text.replace(MARKER_ANYWHERE_RE, '');
}

// ── Where the clinician-visible answer ends ──
// Same story as the markers, one line further down the reply. The contract's
// detail sections follow the answer, wrapped in <details> by the 27B lane and
// emitted loose by the 4B one. The streamer cuts there server-side, so a live
// answer is clean — but the turn is stored whole, and a reopened conversation
// printed "…increased risk. <details> <cited_sources> ["SEP-001"] …" into the
// chat bubble as if the ids were part of the clinical answer. Mirrors the
// server's own `_contract_cut`.
const CONTRACT_TAGS =
  'display_mode|patient_facts_summary|local_policy_guidance|external_guidance|'
  + 'clinical_assessment|caveats|escalation|cited_sources|action_intent';
const CONTRACT_TAIL_RE = new RegExp(`<\\s*/?(?:details|${CONTRACT_TAGS})\\b`, 'i');
const CONTRACT_TAG_RE = new RegExp(`</?(?:details|${CONTRACT_TAGS})[^>]*>`, 'gi');

function cutContractTail(text: string): string {
  const m = text.match(CONTRACT_TAIL_RE);
  const body = m && m.index !== undefined ? text.slice(0, m.index) : text;
  // A tag the model repeated mid-answer, or an unmatched close tag.
  return body.replace(CONTRACT_TAG_RE, '');
}

function sanitizeAssistantContent(rawText: string): string {
  if (!rawText) return '';
  const text = stripContractMarkers(rawText)
    .replace(/^```[a-zA-Z]*\s*\n?/g, '').replace(/\n?```\s*$/g, '');

  // If the text does not contain typical XML tags like concise_answer or clinical_assessment,
  // treat it as a plain text response and render it directly — but only up to
  // where the contract's detail sections begin (they are not the answer).
  if (!/<concise_answer>|<clinical_assessment>/i.test(text)) {
    return cutContractTail(text).trim();
  }

  const extractAndClean = (regex: RegExp) => {
    const match = text.match(regex);
    if (!match) return null;
    let content = match[1];
    
    // 1. If a sibling tag started because the closing tag was missing, truncate at the new tag
    content = content.replace(/<(?:display_mode|patient_facts_summary|local_policy_guidance|external_guidance|clinical_assessment|caveats|escalation|cited_sources|action_intent)[^>]*>[\s\S]*$/gi, '');
    
    // 2. Strip any orphaned/closing tags that might remain
    content = content.replace(/<\/?(?:display_mode|patient_facts_summary|local_policy_guidance|external_guidance|clinical_assessment|caveats|escalation|cited_sources|action_intent|concise_answer)[^>]*>/gi, '');
    
    // 3. Strip trailing malformed/incomplete tags like "<ca" or "<"
    content = content.replace(/<[a-z_]*$/i, '');
    
    return content.trim();
  };

  const concise = extractAndClean(/<concise_answer>([\s\S]*?)(?:<\/concise_answer>|$)/i);
  if (concise) return concise;

  const assessment = extractAndClean(/<clinical_assessment>([\s\S]*?)(?:<\/clinical_assessment>|$)/i);
  if (assessment) return assessment;

  // If safe blocks haven't started streaming yet, suppress all output.
  return '';
}

function formatForDisplay(text: string | undefined): string {
  if (!text) return '';
  const formatted = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/^(\s*)[-*]\s+/gm, '$1• ')
    .replace(/\*\*([^\n*]+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^\s\n*](?:[^\n*]*?[^\s\n*])?)\*/g, '<em>$1</em>')
    .replace(/\n/g, '<br/>');
  // Defense-in-depth: the formatter above only emits <strong>, <em> and <br/>.
  return DOMPurify.sanitize(formatted, { ALLOWED_TAGS: ['strong', 'em', 'br'], ALLOWED_ATTR: [] });
}

// ─── Expander Component ───
// Trust tiers, most trusted first: 1-2 are the hospital's own approved policy,
// 3+ are external guidance. A clinician weighs "our SOP says" very differently
// from "a guideline somewhere says", so the tier is shown, never averaged away.
// Keyed by the server's int tier.
const TRUST_TIER_LABEL: Record<number, string> = {
  1: 'Hospital policy',
  2: 'Hospital formulary',
  3: 'External guideline',
  4: 'Reference',
};

// Who tied each document to the answer (the server's per-source
// `attribution`). The model cites nothing on most turns, so Orb's own match
// (`system_attributed`) is the common case and must be told apart from a
// citation the model made.
const ATTRIBUTION_LABEL: Record<string, string> = {
  model_cited: 'Cited by Sage',
  system_attributed: 'Matched by Orb',
  context_only: 'Retrieved, not used',
};

/** A source row under either declaration: SageSource (sageSources.ts) or the
 *  stream's SageSourceRef (api/client). Both carry the int `trust_tier`. */
type SourceRow = SageSource | SageSourceRef;

function attributionOf(s: SourceRow): string {
  if (s.attribution && ATTRIBUTION_LABEL[s.attribution]) return s.attribution;
  return s.cited ? 'model_cited' : 'context_only';  // a server older than `attribution`
}

const ATTRIBUTION_RANK: Record<string, number> = { model_cited: 0, system_attributed: 1, context_only: 2 };

export function SageSources({ sources, citationBasis }: { sources?: readonly SourceRow[]; citationBasis?: string }) {
  if (!sources || sources.length === 0) return null;

  // Cited first, then Orb's matches, then context the answer did not use.
  const ordered = [...sources].sort(
    (a, b) => ATTRIBUTION_RANK[attributionOf(a)] - ATTRIBUTION_RANK[attributionOf(b)]
      || (b.fusion_score ?? 0) - (a.fusion_score ?? 0)
  );

  return (
    <div className="sage-details-section">
      <div className="sage-details-title">
        <Icon name="book" size={12} /> {sourcesHeading(citationBasis)}
      </div>
      {citationBasis === 'system_attributed' && (
        <p className="sage-sources__caveat">
          Sage did not name a source. Orb matched these documents to the answer by its
          wording. Verify against them before acting.
        </p>
      )}
      {citationBasis === 'retrieved' && (
        // Say this plainly rather than dressing retrieval up as attribution.
        <p className="sage-sources__caveat">
          Sage did not attribute its answer to a specific document. These were retrieved and
          placed in its context. Verify against them before acting.
        </p>
      )}
      <ul className="sage-sources">
        {ordered.map((s) => {
          const attribution = attributionOf(s);
          return (
            <li key={s.document_id} className={`sage-source${attribution !== 'context_only' ? ' sage-source--cited' : ''}`}>
              <span className="sage-source__title">{s.title || s.document_id}</span>
              <span className="sage-source__meta">
                {(s.trust_tier != null && TRUST_TIER_LABEL[s.trust_tier]) || s.source_type || 'Source'}
                {s.source ? ` · ${s.source}` : ''}
                {/* Honesty label: an invented sample, never this hospital's policy. */}
                {s.is_synthetic ? " · sample document, not this hospital's policy" : ''}
              </span>
              <span className="sage-source__badge">{ATTRIBUTION_LABEL[attribution]}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/**
 * The status line while a turn runs, with the seconds since it was sent.
 * Mounted only while there is a stage, so its timer stops when the panel
 * clears the stage. The old line promised "a minute or two", which was not
 * what the appliance measured, and gave no sense of time passing.
 */
export function SageStageStatus({ stage, startedAt }: { stage: 'retrieving' | 'generating'; startedAt: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const seconds = Math.max(0, Math.floor((now - startedAt) / 1000));
  return (
    <div className="sage-stage-status" role="status" aria-live="polite">
      {stage === 'retrieving' ? 'Searching clinical guidelines…' : 'Writing the answer…'}
      {' '}<span aria-hidden="true">{seconds} s</span>
    </div>
  );
}

/** One detail section of a structured answer; `content` is absent when the
 *  model wrote nothing for it. */
interface DetailSection {
  key: string;
  icon: IconName;
  title: string;
  content?: string;
}

interface DetailsExpanderProps {
  sections: DetailSection[];
  defaultExpanded: boolean;
  sources?: readonly SourceRow[];
  citationBasis?: string;
  citedCount?: number;
  attributedCount?: number;
  patientName?: string | null;
  patientContextUsed?: boolean;
  patientSource?: string | null;
  queryScope?: string;
}

function SageDetailsExpander({ sections, defaultExpanded, sources, citationBasis, citedCount, attributedCount, patientName, patientContextUsed, patientSource, queryScope }: DetailsExpanderProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  // Sources alone are worth expanding for, even when the model produced no
  // detail sections — they are the whole point of the evidence trail.
  if (sections.length === 0 && (!sources || sources.length === 0)) return null;

  const showPatientChip = patientContextUsed && patientName && queryScope !== 'GENERAL_MEDICAL';

  return (
    <div style={{ marginTop: '12px' }}>
      <button className="sage-details-btn" onClick={() => setExpanded(!expanded)}>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
          style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform 0.2s ease' }}>
          <polyline points="9 18 15 12 9 6" />
        </svg>
        {expanded ? 'Hide details' : 'Show details'}
        {showPatientChip && (
          <span style={{ color: patientSource === 'discharged' ? 'var(--status-warn)' : 'var(--accent-text)', fontWeight: 600 }}>
            · {patientName}{patientSource === 'discharged' ? ' (discharged)' : ''}
          </span>
        )}
        {sources && sources.length > 0 && (
          // Count what is true. When the model attributed nothing, saying
          // "3 sources" would claim an attribution that never happened.
          <span style={{ color: 'var(--accent-text)' }}>
            · {citationBasis === 'model_cited'
              ? `${citedCount ?? sources.length} cited`
              : citationBasis === 'system_attributed'
                ? `${attributedCount ?? sources.length} matched by Orb`
                : `${sources.length} retrieved`}
          </span>
        )}
      </button>

      {expanded && (
        <div className="sage-details-panel">
          {sections.map((s) => (
            <div key={s.key} className="sage-details-section">
              <div className="sage-details-title">
                <Icon name={s.icon} size={12} /> {s.title}
              </div>
              <div
                className="sage-details-content"
                dangerouslySetInnerHTML={{ __html: formatForDisplay(s.content) }}
              />
            </div>
          ))}
          <SageSources sources={sources} citationBasis={citationBasis} />
        </div>
      )}
    </div>
  );
}

// ─── Action Card Component ───
import { approveHelixOrder, isSafetyAckRequired, type MedicationSafety } from '../../api/client';
import { hasPermission, MEDICATION_PRESCRIBE } from '../../lib/roles';

type OrderFields = { dose: string; route: string; frequency: string };

// The three things an order cannot reach the pharmacy without. Whatever Sage
// left out of its draft is typed here: the card used to send '' and the order
// arrived with no dose to check or dispense against.
const ORDER_FIELDS: { key: keyof OrderFields; label: string; placeholder: string }[] = [
  { key: 'dose', label: 'Dose', placeholder: 'e.g. 500 mg' },
  { key: 'route', label: 'Route', placeholder: 'e.g. Oral' },
  { key: 'frequency', label: 'Frequency', placeholder: 'e.g. TDS' },
];

const CARD_INPUT_STYLE: React.CSSProperties = {
  flex: 1,
  minWidth: 0,
  minHeight: 32,
  padding: '4px 8px',
  font: 'inherit',
  fontSize: '0.85rem',
  color: 'var(--text-primary)',
  background: 'var(--bg-primary)',
  border: '1px solid var(--border-glass)',
  borderRadius: 'var(--radius-sm)',
};

const CARD_NOTE_STYLE: React.CSSProperties = { fontSize: '0.8rem', lineHeight: 1.4, marginTop: 8 };

function andJoin(words: string[]): string {
  return words.length < 2 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}

function actionTitle(actionType: string): string {
  if (actionType === 'add_medication') return 'Medication order';
  if (actionType === 'chart_mutation') return 'Chart change';
  const words = (actionType || '').replace(/_/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Chart action';
}

/** Every line of a safety screen worth reading before sending: allergy and
 *  interaction alerts, the dose check, and why a typed reason was refused. */
function safetyLines(safety: MedicationSafety | null): string[] {
  if (!safety) return [];
  const lines = [...(safety.allergy_alerts || []), ...(safety.interaction_alerts || [])]
    .map(a => a.message)
    .filter(Boolean);
  if (safety.dose_check?.message) lines.push(safety.dose_check.message);
  if (safety.ack_refused) lines.push(safety.ack_refused);
  return lines;
}

/** The model's action intent, parsed from its JSON. Its keys are whatever the
 *  model wrote, which is why the order fields are coerced with String(). */
interface SageActionPayload {
  action_type?: string;
  medication?: string;
  dose?: unknown;
  route?: unknown;
  frequency?: unknown;
  [key: string]: unknown;
}

/** The `action_state` on an answer that asks for a chart change. The stream
 *  types it as an open record (SageStreamMetadata); sage_routes builds it in
 *  three shapes (drafted, not in the formulary, unsupported) and every one
 *  carries these fields. */
interface SageActionState extends Record<string, unknown> {
  action_type: string;
  target_patient: string;
  /** Parsed JSON, or the raw intent string when it was not JSON. */
  requested_payload: SageActionPayload | string;
  status_code: string;
  safe_detail_message: string;
}

function SageActionCard({ actionState }: { actionState: SageActionState }) {
  // A raw string intent reads as an empty payload: every field below is
  // undefined on it, exactly as on {}.
  const payload = (actionState?.requested_payload || {}) as SageActionPayload;
  const [localStatus, setLocalStatus] = useState(actionState?.status_code);
  const [isApproving, setIsApproving] = useState(false);
  const [fields, setFields] = useState<OrderFields>(() => ({
    dose: String(payload.dose ?? '').trim(),
    route: String(payload.route ?? '').trim(),
    frequency: String(payload.frequency ?? '').trim(),
  }));
  // The server's own words when it refuses, kept on the card: a failure used
  // to be logged to the console and the card just said "Failed".
  const [error, setError] = useState<string | null>(null);
  // Open when the send came back 428: the alerts, and the reason being typed.
  const [ack, setAck] = useState<{ safety: MedicationSafety | null; message: string; reason: string } | null>(null);
  const [sentSafety, setSentSafety] = useState<MedicationSafety | null>(null);

  if (!actionState) return null;

  const drafted = localStatus === 'request_drafted';
  const missing = ORDER_FIELDS.filter(f => !fields[f.key].trim());

  const handleApprove = async (ackReason?: string) => {
    setIsApproving(true);
    setError(null);
    try {
      const body = {
        patient_name: actionState.target_patient,
        medication: payload.medication || '',
        dose: fields.dose.trim(),
        route: fields.route.trim(),
        frequency: fields.frequency.trim(),
        ...(ackReason ? { safety_ack: { acknowledged: true as const, reason: ackReason } } : {}),
      };
      const result = await approveHelixOrder(body);
      setSentSafety(result?.safety ?? null);
      setAck(null);
      setLocalStatus('clinician_approved_and_sent_to_helix');
    } catch (e) {
      if (isSafetyAckRequired(e)) {
        setAck(prev => ({ safety: e.safety ?? null, message: e.message, reason: prev?.reason ?? '' }));
      } else {
        setError((e as Error)?.message || 'The order did not reach the pharmacy. Try again.');
      }
    } finally {
      setIsApproving(false);
    }
  };

  const getBadgeConfig = () => {
    switch (localStatus) {
      case 'request_drafted': return { label: 'Drafted', className: 'sage-action-card__badge--warning' };
      case 'not_in_helix_database': return { label: 'Not Available in Pharmacy', className: 'sage-action-card__badge--danger' };
      case 'clinician_approved_and_sent_to_helix': return { label: 'Sent to Pharmacy', className: 'sage-action-card__badge--success' };
      case 'unsupported': return { label: 'Not Executed', className: 'sage-action-card__badge--unsupported' };
      case 'failed': return { label: 'Failed', className: 'sage-action-card__badge--danger' };
      default: return { label: 'Unknown', className: 'sage-action-card__badge--unsupported' };
    }
  };

  const badge = getBadgeConfig();
  const sentLines = safetyLines(sentSafety);
  // The heading already says the refusal when the server sent one; do not
  // print it twice.
  const ackLines = safetyLines(ack?.safety ?? null).filter(line => line !== ack?.message);
  // A severe allergy clears only for a prescriber. The server names the
  // permission; without it there is no reason box, only who can override.
  const prescriberOnly = !!ack?.safety?.override_requires_permission
    && !hasPermission(MEDICATION_PRESCRIBE);
  const isMedicationOrder = actionState.action_type === 'add_medication';

  return (
    <div className="sage-action-card" style={{ borderColor: localStatus === 'clinician_approved_and_sent_to_helix' ? 'var(--status-ok)' : undefined }}>
      <div className="sage-action-card__header">
        <span className="sage-action-card__icon"><Icon name="stethoscope" size={16} /></span>
        <span className="sage-action-card__title">{actionTitle(actionState.action_type)}</span>
        <span className={`sage-action-card__badge ${badge.className}`}>{badge.label}</span>
      </div>
      <div className="sage-action-card__body">
        <div className="sage-action-card__row">
          <span className="sage-action-card__label">Patient:</span>
          <span className="sage-action-card__value">{actionState.target_patient}</span>
        </div>
        {payload.medication && (
          <div className="sage-action-card__row">
            <span className="sage-action-card__label">Medication:</span>
            <span className="sage-action-card__value">{payload.medication}</span>
          </div>
        )}
        {isMedicationOrder && ORDER_FIELDS.map(f => (
          <div className="sage-action-card__row" key={f.key} style={{ alignItems: 'center' }}>
            <span className="sage-action-card__label">{f.label}:</span>
            {drafted ? (
              <input
                style={CARD_INPUT_STYLE}
                value={fields[f.key]}
                placeholder={f.placeholder}
                aria-label={`${f.label} for ${payload.medication || 'this medication'}`}
                disabled={isApproving}
                onChange={ev => {
                  const value = ev.target.value;
                  setFields(prev => ({ ...prev, [f.key]: value }));
                }}
              />
            ) : (
              <span className="sage-action-card__value">{fields[f.key] || 'Not given'}</span>
            )}
          </div>
        ))}
      </div>

      {ack && drafted && (
        <div role="alert" style={{ marginBottom: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: '0.85rem', fontWeight: 650, color: 'var(--status-danger-text)' }}>
            {ack.message || 'Severe drug-safety alerts must be acknowledged with a reason before this order proceeds.'}
          </div>
          {ackLines.length > 0 && (
            <ul style={{ margin: 0, paddingLeft: 18, fontSize: '0.8rem', lineHeight: 1.4, color: 'var(--status-danger-text)' }}>
              {ackLines.map((line, i) => <li key={i}>{line}</li>)}
            </ul>
          )}
          {prescriberOnly ? (
            <>
              <div style={{ fontSize: '0.8rem', color: 'var(--status-danger-text)' }}>
                Only a prescriber can override an allergy block.
              </div>
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                <button type="button" className="btn-glass" onClick={() => setAck(null)}>
                  Close
                </button>
              </div>
            </>
          ) : (<>
          <textarea
            style={{ ...CARD_INPUT_STYLE, minHeight: 56, resize: 'vertical' }}
            value={ack.reason}
            aria-label="Reason for sending this order despite the alerts"
            placeholder="Clinical reason for going ahead (required, recorded with your name)"
            disabled={isApproving}
            onChange={ev => {
              const reason = ev.target.value;
              setAck(prev => (prev ? { ...prev, reason } : prev));
            }}
          />
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <button
              type="button"
              className="sage-action-approve"
              disabled={isApproving || !ack.reason.trim() || missing.length > 0}
              onClick={() => handleApprove(ack.reason.trim())}
            >
              {isApproving ? 'Sending...' : 'Acknowledge and send'}
            </button>
            <button type="button" className="btn-glass" disabled={isApproving} onClick={() => setAck(null)}>
              Cancel
            </button>
          </div>
          </>)}
        </div>
      )}

      <div className="sage-action-card__footer" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <span>
          {drafted && isMedicationOrder
            ? (missing.length > 0
              ? `Enter the ${andJoin(missing.map(f => f.label.toLowerCase()))} before sending.`
              : 'Check the details, then send the order to pharmacy.')
            : actionState.safe_detail_message}
        </span>
        {drafted && !ack && (
          <button
            type="button"
            onClick={() => handleApprove()}
            disabled={isApproving || missing.length > 0}
            className="sage-action-approve"
          >
            {isApproving ? 'Sending...' : 'Send to pharmacy'}
          </button>
        )}
      </div>
      {error && (
        <div role="alert" style={{ ...CARD_NOTE_STYLE, color: 'var(--status-danger-text)' }}>{error}</div>
      )}
      {sentLines.length > 0 && (
        <div style={{ ...CARD_NOTE_STYLE, color: 'var(--status-warn-text)' }}>
          Sent with these alerts: {sentLines.join(' ')}
        </div>
      )}
    </div>
  );
}

// ─── Main Message Component ───
/** What a Sage turn carries in `metadata`: the stream's metadata event, the
 *  parsed sections under `structured`, the voice lane's timing, `status`
 *  'stopped' when the clinician stopped it, or the slimmer set a stored turn
 *  reloads with (SageHistoryMessage). */
export interface SageTurnMetadata extends Omit<SageStreamMetadata, 'timing'> {
  timing?: SageStreamTiming | SageVoiceTiming;
  structured?: SageStructuredResponse;
  status?: 'stopped';
}

/** One message in a Sage thread. */
export interface SageChatMessage {
  role: string;
  content: string;
  metadata?: SageTurnMetadata;
}

interface Props {
  message: SageChatMessage
  isStreaming: boolean
  isSpeaking: boolean
}

export function SageMessage({ message, isStreaming }: Props) {
  const isAssistant = message.role === 'assistant';
  
  const structured = message.metadata?.structured;
  // The wire type leaves action_state an open record; see SageActionState.
  const actionState = message.metadata?.action_state as SageActionState | null | undefined;
  
  // Only sanitize if it's an assistant message. User messages pass through unmodified.
  const visibleText = isAssistant 
    ? (structured?.concise_answer || sanitizeAssistantContent(message.content))
    : message.content;

  const displayMode = message.metadata?.display_mode || 'concise';
  const isDetailed = displayMode === 'detailed';

  const detailSections = structured ? ([
    { key: 'patient_facts', icon: 'clipboard', title: 'Patient Facts', content: structured.patient_facts_summary },
    { key: 'local_policy', icon: 'building', title: 'Local Policy', content: structured.local_policy_guidance },
    { key: 'external_guidance', icon: 'book', title: 'External Guidance', content: structured.external_guidance },
    { key: 'clinical_assessment', icon: 'stethoscope', title: 'Clinical Assessment', content: structured.clinical_assessment },
    { key: 'caveats', icon: 'alert', title: 'Caveats', content: structured.caveats },
    { key: 'escalation', icon: 'siren', title: 'Escalation', content: structured.escalation },
  ] satisfies DetailSection[]).filter(s => s.content && !['none', 'none.', 'no patient context.', 'no local policy retrieved.', 'no external guidance retrieved.'].includes(s.content.toLowerCase().trim())) : [];

  return (
    <div className={`sage-msg sage-msg--${message.role}`}>
      {isAssistant && (
        <div className="sage-msg__avatar">
          <OrbLogo size={20} />
        </div>
      )}
      
      <div className="sage-msg__content">
        {/* Status Banners */}
        {isAssistant && message.metadata && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '4px' }}>
            {message.metadata.status === 'stopped' && (
              <div className="sage-banner sage-banner--stopped">
                <Icon name="pause" size={12} /> Stopped by user
              </div>
            )}
            {message.metadata.escalation && (
              <div className="sage-banner sage-banner--danger">
                <Icon name="alert" size={12} /> High-Risk Query: Verify with attending
              </div>
            )}
            {message.metadata.confidence === 'low' && !actionState && (
              <div className="sage-banner sage-banner--warning">
                <Icon name="search" size={12} /> Limited evidence: Verify independently
              </div>
            )}
            {(message.metadata.data_completeness?.missing?.length ?? 0) > 0 && message.metadata.patient_context_used && !actionState && (
              <div className="sage-banner sage-banner--warning">
                <Icon name="alert" size={12} /> Partial data: {message.metadata.data_completeness?.missing?.join(', ')}
              </div>
            )}
            {/* The primary model was evicted, so this answer came from the
                smaller tier. Never let a lower-capability answer pass as the
                full-strength one. */}
            {message.metadata.provider?.downgraded && (
              <div className="sage-banner sage-banner--warning">
                <Icon name="battery" size={12} /> Answered by the smaller model ({message.metadata.provider.model}),{' '}
                {message.metadata.provider.preferred_model} is not loaded. Verify before acting.
              </div>
            )}
            {/* WHICH BRAIN ANSWERED. Separate from the residency banner above:
                that one says "the 27B is not loaded" (an appliance fact), this
                one says what the thinking toggle and the safety floor did (a
                clinical-effort fact). Both can be true at once, and a clinician
                needs both. Renders nothing for the ordinary careful answer. */}
            {(() => {
              // SageProviderInfo (api/client) and SageProvenance
              // (api/thinkingMode) declare the same `provider` object; the
              // first types preferred_model and thinking_tier more loosely.
              const notice = describeAnswerProvenance(message.metadata.provider as SageProvenance | null | undefined);
              if (!notice) return null;
              const tone =
                notice.kind === 'unsatisfied' ? 'sage-banner--danger'
                  : notice.kind === 'floor' ? 'sage-banner--floor'
                    : 'sage-banner--warning';
              return (
                <div className={`sage-banner ${tone}`}>
                  <Icon name={NOTICE_ICON[notice.kind]} size={12} /> {notice.text}
                </div>
              );
            })()}
          </div>
        )}

        {/* Action Card First (if present) */}
        {isAssistant && actionState && !isStreaming && (
          <SageActionCard actionState={actionState} />
        )}

        {/* Bubble */}
        {/* No opacity here: it dimmed the answer text along with the surface and
            put it under AA. Colour tokens carry the de-emphasis instead. */}
        <div className="sage-msg__bubble" style={actionState && !isStreaming ? { color: 'var(--text-secondary)', fontSize: '0.9em', background: 'var(--bg-glass)', border: 'none', padding: '12px' } : {}}>
          {isStreaming && !visibleText ? (
            <span style={{ color: 'var(--text-muted)', letterSpacing: '0.1em' }}>●●●</span>
          ) : (
            <div className="sage-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{visibleText}</ReactMarkdown></div>
          )}
        </div>

        {isAssistant && !isStreaming && !actionState && (() => {
          const note = noSourcesNote(
            message.metadata?.citation_basis,
            message.metadata?.patient_context_used,
            message.metadata?.query_scope,
            message.metadata?.confidence_basis,
          );
          return note ? <p className="sage-sources__caveat">{note}</p> : null;
        })()}

        {/* Expander — also opens for a turn with no detail sections but real
            retrieved sources, so the evidence trail is never unreachable. */}
        {isAssistant && !isStreaming && (detailSections.length > 0 || (message.metadata?.sources?.length ?? 0) > 0) && (
          <SageDetailsExpander
            sections={detailSections}
            defaultExpanded={isDetailed}
            sources={message.metadata?.sources}
            citationBasis={message.metadata?.citation_basis}
            citedCount={message.metadata?.cited_count}
            attributedCount={message.metadata?.attributed_count}
            patientName={message.metadata?.patient_name}
            patientContextUsed={message.metadata?.patient_context_used}
            patientSource={message.metadata?.patient_source}
            queryScope={message.metadata?.query_scope}
          />
        )}
      </div>
    </div>
  );
}
