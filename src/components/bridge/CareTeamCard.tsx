import { useEffect, useState } from 'react'
import { getCareTeam, type CareTeamMember, type CareTeamResponse } from '../../api/bridgeClient'

/* The server names no individual nurse: Orb records no nurse-to-patient
   assignment, so the nursing entry is the ward's team (is_team), never the
   first nurse on the staff list. */
type TeamEntry = CareTeamMember & { ward?: string | null; is_team?: boolean }

/** Shows the patient who is looking after them — attending physician, nurse, and unit. */
export function CareTeamCard() {
  const [data, setData] = useState<CareTeamResponse | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    getCareTeam()
      .then(d => { if (alive) setData(d) })
      .catch(e => { if (alive) setError(e.message) })
    return () => { alive = false }
  }, [])

  return (
    <div className="bridge__card glass-card">
      <div className="bridge__card-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
          <circle cx="9" cy="7" r="4" />
          <path d="M23 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />
        </svg>
      </div>
      <h2 className="bridge__card-title">Your Care Team</h2>

      {error && <p className="bridge__card-text">Unable to load your care team right now.</p>}

      {!error && !data && <p className="bridge__card-text">Loading…</p>}

      {data && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 4 }}>
          {data.care_team.length === 0 && (
            <p className="bridge__card-text">Your care team will appear here once assigned.</p>
          )}
          {data.care_team.map((m: TeamEntry, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div style={{
                width: 40, height: 40, borderRadius: '50%', flexShrink: 0,
                display: 'grid', placeItems: 'center',
                background: 'var(--accent-glow)', border: '1px solid var(--border-focus)',
                // Initials are read, not glanced at — --accent-text is the token
                // tuned for that job; --accent-gold measured 3.25:1 on this wash.
                color: 'var(--accent-text)', fontWeight: 700, fontSize: '0.95rem',
              }}>
                {m.name.replace(/^Dr\.?\s*/i, '').charAt(0)}
              </div>
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{m.name}</div>
                <div style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
                  {m.is_team
                    ? [m.ward ? `Ward ${m.ward}` : null, m.department].filter(Boolean).join(' · ')
                    : `${m.role}${m.department ? ` · ${m.department}` : ''}`}
                </div>
              </div>
            </div>
          ))}

          {(data.unit.ward || data.unit.bed) && (
            <div style={{
              marginTop: 4, paddingTop: 10, borderTop: '1px solid var(--border-subtle)',
              fontSize: '0.82rem', color: 'var(--text-secondary)',
            }}>
              {/* Each part stays on one line: as one text run the browser broke
                  "Bed SW-102" after the hyphen. A missing department is left out. */}
              Care unit:{' '}
              {[
                data.unit.department,
                data.unit.ward ? `Ward ${data.unit.ward}` : null,
                data.unit.bed ? `Bed ${data.unit.bed}` : null,
              ].filter(Boolean).map((part, i) => (
                <span key={i}>
                  {i > 0 && ' · '}
                  <span style={{ whiteSpace: 'nowrap' }}>{part}</span>
                </span>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
