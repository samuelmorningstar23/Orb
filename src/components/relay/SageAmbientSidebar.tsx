import { useState, useEffect, useCallback } from 'react';
import { getSageMemory, getSageActions, transitionSageAction, socket, type SageAmbientMemory, type SageAction } from '../../api/client';
import styles from './SageAmbientSidebar.module.css';
import { summaryRows } from './ambientSummary';

interface Props {
  channelId: string;
  /** Below 900px the panel is a slide-over drawer, off screen unless open. */
  open?: boolean;
  /** Given by the room: shows a Close control inside the drawer, since the
   *  drawer covers the header button that opened it on a phone. */
  onClose?: () => void;
}

const CONFIDENCE_THRESHOLD_RISK = 0.80;
const STALE_THRESHOLD_MS = 30 * 60 * 1000; // 30 minutes

export default function SageAmbientSidebar({ channelId, open = false, onClose }: Props) {
  const [memory, setMemory] = useState<SageAmbientMemory | null>(null);
  const [actions, setActions] = useState<SageAction[]>([]);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [loadingActionId, setLoadingActionId] = useState<string | null>(null);
  // Why the last action update failed, shown on that card. It was a browser
  // alert() that blocked the room and was gone once dismissed.
  const [actionError, setActionError] = useState<{ id: string; message: string } | null>(null);

  const fetchMemory = useCallback(async () => {
    try {
      const [memData, actData] = await Promise.all([
        getSageMemory(channelId),
        getSageActions(channelId)
      ]);
      setMemory(memData);
      setActions(actData);
      setFailed(false);
    } catch (err) {
      console.error('Failed to fetch Sage memory:', err);
      // A failure used to be console-only. `memory` stayed null, the visibility
      // check below returned null, and the whole risk-flag panel simply was not
      // there — indistinguishable from "Sage has flagged no risks in this room".
      // An absent deterioration warning that reads as an all-clear is the one
      // failure mode this panel must never have. Say it broke.
      setFailed(true);
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
    setLoading(true);
    fetchMemory();

    const handleUpdate = (data: { room_id: string; updated_at: string }) => {
      if (data.room_id === channelId) {
        // Trigger a silent refetch to get authoritative state
        fetchMemory();
      }
    };

    const handleActionUpdate = (data: { channel_id: string }) => {
      if (data.channel_id === channelId) {
        getSageActions(channelId).then(setActions).catch(console.error);
      }
    };

    socket.on('sage:memory_update', handleUpdate);
    socket.on('sage:action_update', handleActionUpdate);

    return () => {
      socket.off('sage:memory_update', handleUpdate);
      socket.off('sage:action_update', handleActionUpdate);
    };
  }, [channelId, fetchMemory]);

  const handleTransition = async (actionId: string, state: SageAction['state'], version: number) => {
    setLoadingActionId(actionId);
    setActionError(null);
    try {
      const res = await transitionSageAction(channelId, actionId, state, version);
      // Optimistic update. The handoff comes from THIS response, not only the
      // socket refresh: a confirm the drug-safety screen stopped must read as
      // blocked even when the socket is down, never as a bare "confirmed".
      setActions(prev => prev.map(a => a.id === actionId
        ? { ...a, state, version: version + 1, handoff: res.handoff ?? a.handoff }
        : a));
    } catch (err) {
      setActionError({ id: actionId, message: (err as Error)?.message || 'This action could not be updated. Try again.' });
      getSageActions(channelId).then(setActions).catch(console.error);
    } finally {
      setLoadingActionId(null);
    }
  };

  // `.open` is the drawer's on-screen state (the module's <=900px rule); above
  // 900px the class does nothing and the panel sits beside the thread.
  const rootClass = open ? `${styles.sidebar} ${styles.open}` : styles.sidebar;
  const closeBtn = onClose ? (
    <button type="button" className="relay-sage-drawer__close" onClick={onClose}>
      Close
    </button>
  ) : null;

  if (loading && !memory) {
    return (
      <div className={rootClass}>
        {closeBtn}
        <div className={styles.emptyState}>
          <span className={styles.loading}>Connecting to Sage...</span>
        </div>
      </div>
    );
  }

  // A load failure is never hidden — see fetchMemory.
  if (failed && !memory) {
    return (
      <div className={rootClass}>
        <div className={styles.header}>Sage Copilot{closeBtn}</div>
        <div className={styles.failState} role="alert">
          <p>
            Sage Copilot could not load for this room. This is a display failure.
            It does not mean there are no risk flags.
          </p>
          <button type="button" className={styles.retryBtn} onClick={fetchMemory}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  // Determine visibility. Action cards render with or without a room memory:
  // a card drafted into a room Sage has not summarised yet (a Sage insight's
  // one-click draft lands exactly there) used to be invisible, because the
  // whole panel returned null whenever the memory row was missing.
  const hasMemory = !!memory && (!!memory.discussion_summary || !!memory.current_concern || !!memory.risk_flags?.length);
  if (!hasMemory && !actions.length) {
    // Hidden beside the thread. A drawer the reader has just opened must not
    // come up blank, so it says what the empty state means.
    if (!open || !onClose) return null;
    return (
      <div className={rootClass}>
        <div className={styles.header}>Sage Copilot{closeBtn}</div>
        <div className={styles.emptyState}>
          <span>Sage has not summarised this room yet.</span>
        </div>
      </div>
    );
  }

  const now = new Date().getTime();
  const isStale = (dateStr: string) => (now - new Date(dateStr).getTime()) > STALE_THRESHOLD_MS;

  const validRisks = (memory?.risk_flags || []).filter(r => r.confidence >= CONFIDENCE_THRESHOLD_RISK);
  
  const activeActions = actions.filter(a => ['suggested', 'acknowledged'].includes(a.state));
  const handledActions = actions.filter(a => ['dismissed', 'confirmed'].includes(a.state));

  return (
    <div className={rootClass}>
      <div className={styles.header}>
        <svg className={styles.icon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
          <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 3.104v5.714a2.25 2.25 0 01-.659 1.591L5 14.5M9.75 3.104c-.251.023-.501.05-.75.082m.75-.082a24.301 24.301 0 014.5 0m0 0v5.714c0 .597.237 1.17.659 1.591L19.8 15.3M14.25 3.104c.251.023.501.05.75.082M19.8 15.3l-1.57.393A9.065 9.065 0 0112 15a9.065 9.065 0 00-6.23-.693L4.2 15.3m15.6 0v1.47a2.25 2.25 0 01-1.372 2.068l-5.69 2.276a2.25 2.25 0 01-1.676 0l-5.69-2.276a2.25 2.25 0 01-1.372-2.068v-1.47" />
        </svg>
        Sage Copilot
        {closeBtn}
      </div>

      {memory && (memory.current_concern || memory.discussion_summary) && (
        <div className={styles.summaryBlock}>
          {memory.current_concern && <div className={styles.concern}>{memory.current_concern}</div>}
          {/* One row per update, the server's retraction note labelled: as one
              paragraph a retracted result read as current (see ambientSummary.ts). */}
          {memory.discussion_summary && (
            <ul className={styles.summaryText} style={{ listStyle: 'none', margin: 0, padding: 0, display: 'flex', flexDirection: 'column', gap: 4 }}>
              {summaryRows(memory.discussion_summary).map((row, i) => (
                <li key={i}>
                  {row.correction && (
                    <span style={{ display: 'inline-block', marginRight: 6, padding: '0 6px', borderRadius: 999, fontSize: '0.7rem', fontWeight: 700, color: 'var(--status-warn-text)', border: '1px solid color-mix(in srgb, var(--status-warn) 28%, transparent)', background: 'color-mix(in srgb, var(--status-warn) 10%, transparent)' }}>
                      Correction
                    </span>
                  )}
                  {row.text}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {validRisks.length > 0 && (
        <>
          <div className={styles.sectionTitle}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
            Risk Flags
          </div>
          {validRisks.map((risk, idx) => {
            const stale = isStale(risk.updated_at);
            return (
              <div key={idx} className={`${styles.card} ${styles.riskCard} ${stale ? styles.stale : ''}`}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>Risk Detected</span>
                  <span className={`${styles.badge} ${stale ? styles.staleBadge : styles.riskBadge}`}>
                    {stale ? 'Stale' : 'Active'}
                  </span>
                </div>
                <p className={styles.cardBody}>{risk.detail}</p>
                {risk.rationale && (
                  <p className={styles.cardBody} style={{ marginTop: '6px', fontSize: '0.8rem' }}>
                    <strong>Signal:</strong> {risk.rationale}
                  </p>
                )}
              </div>
            );
          })}
        </>
      )}

      {activeActions.length > 0 && (
        <>
          <div className={styles.sectionTitle}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
            Suggested Actions
          </div>
          {activeActions.map((intent) => {
            const stale = isStale(intent.updated_at);
            const isProcessing = loadingActionId === intent.id;
            return (
              <div key={intent.id} className={`${styles.card} ${stale ? styles.stale : ''}`}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>{intent.action_type.replace(/_/g, ' ').toUpperCase()}</span>
                  <span className={`${styles.badge} ${stale ? styles.staleBadge : styles.suggestionBadge}`}>
                    {stale ? 'Stale' : intent.state}
                  </span>
                </div>
                <p className={styles.cardBody}>Target: {intent.target}</p>
                {intent.rationale && (
                  <p className={styles.cardBody} style={{ marginTop: '6px', fontSize: '0.8rem' }}>
                    <strong>Signal:</strong> {intent.rationale}
                  </p>
                )}
                {actionError?.id === intent.id && (
                  <p className={styles.handoffReason} role="alert">{actionError.message}</p>
                )}
                <div className={styles.actionRow}>
                  {intent.state === 'suggested' && (
                    <button className={`${styles.btn} ${styles.btnAck}`} onClick={() => handleTransition(intent.id, 'acknowledged', intent.version)} disabled={isProcessing}>
                      {isProcessing ? '...' : 'Acknowledge'}
                    </button>
                  )}
                  <button className={`${styles.btn} ${styles.btnDismiss}`} onClick={() => handleTransition(intent.id, 'dismissed', intent.version)} disabled={isProcessing}>
                    {isProcessing ? '...' : 'Dismiss'}
                  </button>
                  <button className={`${styles.btn} ${styles.btnConfirm}`} onClick={() => handleTransition(intent.id, 'confirmed', intent.version)} disabled={isProcessing}>
                    {isProcessing ? '...' : 'Confirm'}
                  </button>
                </div>
              </div>
            );
          })}
        </>
      )}

      {handledActions.length > 0 && (
        <>
          <div className={styles.sectionTitle}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7"/></svg>
            Handled Actions
          </div>
          {handledActions.map((intent) => {
            return (
              <div key={intent.id} className={`${styles.card} ${styles.handledCard}`}>
                <div className={styles.cardHeader}>
                  <span className={styles.cardTitle}>{intent.action_type.replace(/_/g, ' ').toUpperCase()}</span>
                  <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                    {intent.handoff && (
                      <span className={`${styles.badge} ${
                        intent.handoff.status === 'accepted' && intent.handoff.downstream_ref_id === 'charted-only' ? styles.handoffChartedOnly :
                        intent.handoff.status === 'accepted' ? styles.handoffAccepted :
                        intent.handoff.status === 'pending' ? styles.handoffPending :
                        intent.handoff.status === 'failed' ? styles.handoffFailed :
                        intent.handoff.status === 'blocked' ? styles.handoffBlocked :
                        styles.handoffUnsupported
                      }`}>
                        {(() => {
                          const mod = intent.handoff.target_module;
                          const modLabel = mod === 'helix' ? 'Helix' : mod === 'admissions' ? 'Transfer' : mod === 'scribe' ? 'Scribe' : mod;
                          // 'charted-only': on the chart for review, but no pharmacy order exists.
                          return intent.handoff.status === 'accepted' && intent.handoff.downstream_ref_id === 'charted-only' ? 'Charted only' :
                                 intent.handoff.status === 'accepted' ? `Sent to ${modLabel}` :
                                 intent.handoff.status === 'pending' ? `${modLabel} handoff pending` :
                                 intent.handoff.status === 'failed' ? `${modLabel} handoff failed` :
                                 intent.handoff.status === 'blocked' ? 'Blocked' :
                                 'Unsupported route';
                        })()}
                      </span>
                    )}
                    <span className={`${styles.badge} ${styles.handledBadge}`}>
                      {intent.state}
                    </span>
                  </div>
                </div>
                <p className={styles.cardBody}>Target: {intent.target}</p>
                {/* What the drug-safety screen said when this was confirmed. A
                    stopped order says why; one that went through still shows
                    every alert, since the screen only blocks on severe. */}
                {intent.handoff?.status === 'blocked' && intent.handoff.error_summary && (
                  <p className={styles.handoffReason} role="alert">{intent.handoff.error_summary}</p>
                )}
                {intent.handoff?.status === 'accepted' && intent.handoff.downstream_ref_id === 'charted-only' && intent.handoff.error_summary && (
                  <p className={styles.handoffChartedNote}>{intent.handoff.error_summary}</p>
                )}
                {intent.handoff?.status !== 'blocked' && !!intent.handoff?.safety?.alerts.length && (
                  <div className={styles.handoffAlerts}>
                    <span className={styles.handoffAlertsTitle}>Drug-safety alerts when confirmed</span>
                    <ul>
                      {intent.handoff.safety.alerts.map((a, i) => (
                        <li key={i}><strong>{a.severity}</strong> {a.message}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {intent.handoff?.status !== 'blocked' && intent.handoff?.safety?.coverage_note && (
                  <p className={styles.handoffCoverage}>{intent.handoff.safety.coverage_note}</p>
                )}
              </div>
            );
          })}
        </>
      )}
    </div>
  );
}
