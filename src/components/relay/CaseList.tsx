import { useState } from 'react';
import type { Channel } from '../../api/client';
import './RelayComponents.css';

interface CaseListProps {
  channels: Channel[];
  onSelectChannel: (channelId: string) => void;
  emptyMessage?: string;
  title?: string;
}

export default function CaseList({ channels, onSelectChannel, emptyMessage = "No case rooms found.", title = "Case Rooms" }: CaseListProps) {
  const [expanded, setExpanded] = useState(false);

  // We show 10 by default
  const DEFAULT_LIMIT = 10;
  const isTruncated = channels.length > DEFAULT_LIMIT;
  
  // Sort logic is expected to be handled by the parent before passing to CaseList
  const visibleChannels = expanded ? channels : channels.slice(0, DEFAULT_LIMIT);

  return (
    <div className="orb-section">
      {title && <h2 className="orb-section-title">{title}</h2>}
      
      {channels.length === 0 ? (
        <div className="relay-empty-state">{emptyMessage}</div>
      ) : (
        <div className="relay-channel-list">
          {visibleChannels.map(ch => {
            // Fill tokens for the chip's tint and border, -text tokens for its
            // small label: the fill tokens fall short of contrast as 10px text.
            const riskTone = ch.risk_level === 'critical' ? 'danger'
              : ch.risk_level === 'elevated' ? 'warn'
              : 'ok';
            const riskLabel = ch.risk_level === 'critical' ? 'CRITICAL'
              : ch.risk_level === 'elevated' ? 'ELEVATED'
              : ch.risk_level ? 'STABLE' : null;
            const { ward, bed } = ch;

            /* A grid row, not space-between over three loose blocks: that put
               the ward/diagnosis line at a different x on every row and wrapped
               names one word per line on a phone. Fixed alert-dot slot, a main
               column that takes the width, and a fixed badge column, so every
               name starts on the same line whether or not the row has a dot or
               an unread count. */
            return (
              <button key={ch.id} className="relay-channel-item relay-channel-item--case glass-card" onClick={() => onSelectChannel(ch.id)}>
                <span className="relay-case__dot" aria-hidden="true">
                  {ch.has_alert && <span className="relay-channel-item__alert-dot" />}
                </span>
                <span className="relay-case__main">
                  <span className="relay-case__title-row">
                    <span className="relay-channel-item__name">{ch.name}</span>
                    {riskLabel && (
                      <span className="relay-case__risk" style={{
                        color: `var(--status-${riskTone}-text)`,
                        border: `1px solid color-mix(in srgb, var(--status-${riskTone}) 28%, transparent)`,
                        background: `color-mix(in srgb, var(--status-${riskTone}) 8%, transparent)`,
                      }}>{riskLabel}</span>
                    )}
                  </span>
                  {(ward || ch.diagnosis) && (
                    <span className="relay-case__meta">
                      {ward && <span>{ward}{bed ? ` / ${bed}` : ''}</span>}
                      {ch.diagnosis && <span className="relay-case__diagnosis" title={ch.diagnosis}>{ch.diagnosis}</span>}
                    </span>
                  )}
                </span>
                {/* Capped at 99+. A four-digit unread badge is not information:
                    nobody reads 5,428 messages, and printing the exact number
                    only invites the question of where it came from. */}
                <span className="relay-case__badge-slot">
                  {ch.unread_count > 0 && (
                    <span
                      className="relay-channel-item__badge"
                      title={`${ch.unread_count} unread`}
                    >
                      {ch.unread_count > 99 ? '99+' : ch.unread_count}
                    </span>
                  )}
                </span>
              </button>
            );
          })}
          
          {isTruncated && (
            <button 
              className="relay-expand-btn" 
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? 'Show Less ↑' : `Show ${channels.length - DEFAULT_LIMIT} More ↓`}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
