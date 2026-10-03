import { useState, useEffect } from 'react';
import { getAlerts, type Message, type Channel } from '../../api/client';
import './RelayComponents.css';

interface AlertsSectionProps {
  onSelectChannel: (channelId: string) => void;
  channels: Channel[];
}

export default function AlertsSection({ onSelectChannel, channels }: AlertsSectionProps) {
  const [alerts, setAlerts] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    loadAlerts();
  }, []);

  const loadAlerts = async () => {
    setLoading(true);
    setLoadError(false);
    try {
      const data = await getAlerts();
      setAlerts(data);
    } catch (err) {
      console.error('[Relay] Failed to load alerts:', err);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  };

  if (loading) return null;
  if (loadError) {
    return (
      <div className="orb-section relay-alerts-section">
        <p style={{ fontSize: '0.85rem', color: 'var(--text-muted)' }}>
          Alerts could not be loaded.{' '}
          <button
            onClick={loadAlerts}
            style={{ background: 'none', border: 'none', padding: 0, color: 'var(--text-secondary)', textDecoration: 'underline', cursor: 'pointer', font: 'inherit' }}
          >
            Retry
          </button>
        </p>
      </div>
    );
  }
  if (alerts.length === 0) return null;

  // Deduplicate structured alerts by surgery_id (same alert may exist in dept + case room)
  const seen = new Set<string>();
  const dedupedAlerts = alerts.filter(alert => {
    try {
      const payload = JSON.parse(alert.content);
      if (payload?.is_structured && payload?.surgery_id) {
        if (seen.has(payload.surgery_id)) return false;
        seen.add(payload.surgery_id);
      }
    } catch (e) {
      console.error('[Relay] Failed to parse alert content during dedup:', e);
    }
    return true;
  });

  if (dedupedAlerts.length === 0) return null;

  return (
    <div className="orb-section relay-alerts-section">
      <h2 className="orb-section-title" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span className="relay-alert-icon">⚠️</span> Action Required
      </h2>
      <div className="relay-channel-list">
        {dedupedAlerts.map((alert) => {
          let payload: any = null;
          try {
            payload = JSON.parse(alert.content);
          } catch (e) {
            console.error('[Relay] Failed to parse alert content:', e);
          }

          const isStructured = payload && payload.is_structured;
          const title = isStructured ? payload.title : 'Alert';
          const desc = isStructured ? payload.action_required : alert.content;
          
          let targetChannelId = alert.channel_id;
          if (isStructured && payload.patient_id) {
            const caseRoom = channels.find(c => c.type === 'case_room' && c.patient_id === payload.patient_id);
            if (caseRoom) {
              targetChannelId = caseRoom.id;
            }
          }

          return (
            <button 
              key={alert.id} 
              className="relay-channel-item glass-card relay-alert-item"
              onClick={() => onSelectChannel(targetChannelId)}
            >
              <div className="relay-channel-item__info">
                <span className="relay-channel-item__alert-dot relay-alert-dot--urgent" />
                <div className="relay-alert-content">
                  <span className="relay-alert-title">{title}</span>
                  <span className="relay-alert-desc">{desc}</span>
                </div>
              </div>
              <span className="relay-alert-arrow">→</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
