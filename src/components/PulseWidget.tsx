/**
 * Orb Pulse Widget — Environmental Intelligence Overlay
 * 
 * Glassmorphic floating panel triggered from the header.
 * Requests geolocation access for location-specific data.
 * Displays: Weather, AQI, CDC Flu, FDA Recalls, Clinical Alerts.
 * 
 * Data flows IN only — zero patient data egress.
 */

import { useState, useEffect, useCallback, useRef } from 'react'
import { apiFetch } from '../api/client'
import PulseDetail from './PulseDetail'
import { activatable } from '../lib/a11y'
import './PulseWidget.css'

interface PulseData {
  weather: any
  air_quality: any
  flu_surveillance: any
  drug_recalls: any[]
  active_alerts: any[]
  alert_count: number
  last_refresh: string
}

interface GeoLocation {
  lat: number
  lon: number
  label: string
}

// Weather code → emoji mapping (WMO codes)
const weatherEmoji: Record<number, string> = {
  0: '☀️', 1: '🌤️', 2: '⛅', 3: '☁️',
  45: '🌫️', 48: '🌫️',
  51: '🌦️', 53: '🌧️', 55: '🌧️',
  61: '🌦️', 63: '🌧️', 65: '🌧️',
  71: '🌨️', 73: '🌨️', 75: '❄️',
  80: '🌦️', 81: '🌧️', 82: '⛈️',
  95: '⛈️', 96: '⛈️', 99: '⛈️',
}

const DEFAULT_LOCATION: GeoLocation = { lat: 19.076, lon: 72.8777, label: 'Mumbai' }

export default function PulseWidget() {
  const [isOpen, setIsOpen] = useState(false)
  const [pulseData, setPulseData] = useState<PulseData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [location, setLocation] = useState<GeoLocation>(() => {
    const saved = localStorage.getItem('orb_pulse_location')
    return saved ? JSON.parse(saved) : DEFAULT_LOCATION
  })
  const [geoStatus, setGeoStatus] = useState<'idle' | 'requesting' | 'granted' | 'denied'>('idle')
  const [detailView, setDetailView] = useState<string | null>(null)
  const [selectedAlert, setSelectedAlert] = useState<any>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  // Reset detail view when panel closes
  const closePanel = () => { setIsOpen(false); setDetailView(null); setSelectedAlert(null) }
  const openDetail = (view: string) => setDetailView(view)
  const openAlertDetail = (alert: any) => { setSelectedAlert(alert); setDetailView('alert') }

  // ── Geolocation ──
  const requestLocation = useCallback(() => {
    if (!navigator.geolocation) {
      setGeoStatus('denied')
      return
    }

    setGeoStatus('requesting')
    navigator.geolocation.getCurrentPosition(
      async (position) => {
        const newLoc: GeoLocation = {
          lat: position.coords.latitude,
          lon: position.coords.longitude,
          label: 'Current Location',
        }

        // Reverse geocode to get city name
        try {
          const resp = await fetch(
            `https://nominatim.openstreetmap.org/reverse?lat=${newLoc.lat}&lon=${newLoc.lon}&format=json&zoom=10`
          )
          if (resp.ok) {
            const data = await resp.json()
            const city = data.address?.city || data.address?.town || data.address?.county || 'Current Location'
            newLoc.label = city
          }
        } catch {
          // Keep "Current Location" as fallback
        }

        setLocation(newLoc)
        localStorage.setItem('orb_pulse_location', JSON.stringify(newLoc))
        setGeoStatus('granted')
      },
      () => {
        setGeoStatus('denied')
      },
      { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 }
    )
  }, [])

  // Auto-request location on first open
  useEffect(() => {
    if (isOpen && geoStatus === 'idle') {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      requestLocation()
    }
  }, [isOpen, geoStatus, requestLocation])

  // ── Fetch Pulse Data ──
  const fetchPulse = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const data = await apiFetch<PulseData>(
        `/pulse/dashboard?lat=${location.lat}&lon=${location.lon}`
      )
      setPulseData(data)
    } catch (e: any) {
      setError(e.message || 'Failed to fetch environmental data')
    } finally {
      setLoading(false)
    }
  }, [location.lat, location.lon])

  // Fetch when opened or location changes
  useEffect(() => {
    if (isOpen) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      fetchPulse()
    }
  }, [isOpen, fetchPulse])

  // Auto-refresh every 15 min while open
  useEffect(() => {
    if (!isOpen) return
    const interval = setInterval(fetchPulse, 15 * 60 * 1000)
    return () => clearInterval(interval)
  }, [isOpen, fetchPulse])

  // Close on escape
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (detailView) { setDetailView(null); setSelectedAlert(null) }
        else closePanel()
      }
    }
    if (isOpen) {
      document.addEventListener('keydown', handleKey)
      return () => document.removeEventListener('keydown', handleKey)
    }
  }, [isOpen, detailView])

  // Determine alert severity for badge
  const highestSeverity = pulseData?.active_alerts?.[0]?.severity || null

  const getAqiColorClass = (color: string) => {
    const map: Record<string, string> = {
      green: 'pulse-aqi__value--good',
      yellow: 'pulse-aqi__value--moderate',
      orange: 'pulse-aqi__value--orange',
      red: 'pulse-aqi__value--red',
      purple: 'pulse-aqi__value--purple',
      maroon: 'pulse-aqi__value--maroon',
    }
    return map[color] || ''
  }

  return (
    <>
      {/* Trigger Button */}
      <button
        className={`pulse-trigger ${isOpen ? 'pulse-trigger--active' : ''}`}
        onClick={() => isOpen ? closePanel() : setIsOpen(true)}
        title="Orb Pulse: Environmental Intelligence"
        id="btn-pulse"
      >
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
        </svg>

        {/* Alert badge */}
        {pulseData && pulseData.alert_count > 0 && (
          <span
            className={`pulse-trigger__badge ${
              highestSeverity === 'moderate' ? 'pulse-trigger__badge--moderate' : ''
            }`}
          />
        )}
      </button>

      {/* Backdrop + Panel */}
      {isOpen && (
        <>
          <div className="pulse-backdrop" onClick={closePanel} />
          <div className="pulse-panel" ref={panelRef}>
            {/* Header */}
            <div className="pulse-panel__header">
              <div className="pulse-panel__title">
                <svg className="pulse-panel__title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v4" />
                  <path d="M12 18v4" />
                  <path d="M4.93 4.93l2.83 2.83" />
                  <path d="M16.24 16.24l2.83 2.83" />
                  <path d="M2 12h4" />
                  <path d="M18 12h4" />
                </svg>
                Orb Pulse
              </div>
              <button
                className="pulse-panel__location"
                onClick={requestLocation}
                title="Click to update location"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z" />
                  <circle cx="12" cy="10" r="3" />
                </svg>
                {geoStatus === 'requesting' ? 'Locating...' : location.label}
              </button>
            </div>

            {/* Body */}
            <div className="pulse-panel__body">
              {loading && !pulseData ? (
                <div className="pulse-loading">
                  <div className="pulse-loading__spinner" />
                  Fetching environmental data...
                </div>
              ) : error && !pulseData ? (
                <div className="pulse-error">{error}</div>
              ) : pulseData ? (
                <>
                  {/* Active Alerts */}
                  {pulseData.active_alerts.length > 0 ? (
                    <>
                      <div className="pulse-section-label">Active Clinical Alerts<span className="pulse-section-source">Orb Pulse Engine</span></div>
                      {pulseData.active_alerts.map((alert, i) => (
                        <div key={i} className={`pulse-alert pulse-alert--${alert.severity}`} {...activatable(() => openAlertDetail(alert), `Open alert detail: ${alert.title}`)}>
                          <div className="pulse-alert__icon">
                            {alert.severity === 'high' ? (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--status-danger)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
                                <line x1="12" y1="9" x2="12" y2="13" />
                                <line x1="12" y1="17" x2="12.01" y2="17" />
                              </svg>
                            ) : (
                              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--status-warn)" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                                <circle cx="12" cy="12" r="10" />
                                <line x1="12" y1="8" x2="12" y2="12" />
                                <line x1="12" y1="16" x2="12.01" y2="16" />
                              </svg>
                            )}
                          </div>
                          <div className="pulse-alert__content">
                            <div className="pulse-alert__title">{alert.title}</div>
                            <div className="pulse-alert__message">{alert.message}</div>
                          </div>
                        </div>
                      ))}
                    </>
                  ) : (
                    <div className="pulse-no-alerts">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                        <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
                        <polyline points="22 4 12 14.01 9 11.01" />
                      </svg>
                      No active environmental alerts
                    </div>
                  )}

                  <div className="pulse-divider" />

                  {/* Weather */}
                  {pulseData.weather?.current?.temperature != null && (
                    <div className="pulse-weather">
                      <div className="pulse-section-label">Weather<span className="pulse-section-source">Open-Meteo</span></div>
                      <div className="pulse-weather__current" {...activatable(() => openDetail('weather'), 'Open weather detail')}>
                        <div className="pulse-weather__temp">
                          {Math.round(pulseData.weather.current.temperature)}
                          <span>°C</span>
                        </div>
                        <div className="pulse-weather__details">
                          <div className="pulse-weather__desc">
                            {weatherEmoji[pulseData.weather.current.weather_code] || '🌡️'}{' '}
                            {pulseData.weather.current.description}
                          </div>
                          <div className="pulse-weather__meta">
                            Feels like {Math.round(pulseData.weather.current.feels_like || 0)}°C
                            {' · '}
                            {pulseData.weather.current.humidity}% humidity
                          </div>
                        </div>
                      </div>

                      {/* 7-day forecast strip */}
                      {pulseData.weather.forecast?.length > 0 && (
                        <div className="pulse-weather__forecast">
                          {pulseData.weather.forecast.map((day: any, i: number) => (
                            <div key={i} className="pulse-forecast-day">
                              <span className="pulse-forecast-day__label">
                                {i === 0 ? 'Today' : day.day_label?.split(' ')[0]}
                              </span>
                              <span className="pulse-forecast-day__icon">
                                {weatherEmoji[day.weather_code] || '🌡️'}
                              </span>
                              <div className="pulse-forecast-day__temps">
                                <span className="pulse-forecast-day__high">{Math.round(day.temp_max)}°</span>
                                <span className="pulse-forecast-day__low">{Math.round(day.temp_min)}°</span>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}

                  <div className="pulse-divider" />

                  {/* Air Quality */}
                  {pulseData.air_quality?.aqi != null && (
                    <>
                      <div className="pulse-section-label">Air Quality<span className="pulse-section-source">Open-Meteo AQI</span></div>
                      <div className="pulse-aqi" {...activatable(() => openDetail('aqi'), 'Open air quality detail')}>
                        <div className={`pulse-aqi__value ${getAqiColorClass(pulseData.air_quality.color)}`}>
                          {pulseData.air_quality.aqi}
                        </div>
                        <div className="pulse-aqi__info">
                          <div className="pulse-aqi__category">{pulseData.air_quality.category}</div>
                          <div className="pulse-aqi__label">
                            US AQI · PM2.5: {pulseData.air_quality.pollutants?.pm2_5?.toFixed(1) || '—'}
                          </div>
                        </div>
                      </div>
                    </>
                  )}

                  <div className="pulse-divider" />

                  {/* CDC Flu */}
                  {pulseData.flu_surveillance && (
                    <>
                      <div className="pulse-section-label">Flu Surveillance<span className="pulse-section-source">CDC FluView</span></div>
                      <div className="pulse-flu" {...activatable(() => openDetail('flu'), 'Open flu surveillance detail')}>
                        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                          <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
                        </svg>
                        <div>
                          <div className={`pulse-flu__level pulse-flu__level--${pulseData.flu_surveillance.activity_level}`}>
                            {pulseData.flu_surveillance.activity_level?.replace('_', ' ')} Activity
                          </div>
                          <div className="pulse-flu__detail">
                            ILI: {pulseData.flu_surveillance.ili_percentage}%
                            {pulseData.flu_surveillance.source?.includes('Estimate') && ' (seasonal estimate)'}
                          </div>
                        </div>
                      </div>
                    </>
                  )}

                  {/* FDA Recalls */}
                  {pulseData.drug_recalls?.length > 0 && (
                    <>
                      <div className="pulse-divider" />
                      <div className="pulse-section-label">Recent FDA Drug Recalls<span className="pulse-section-source">openFDA</span></div>
                      <div className="pulse-recalls">
                        {pulseData.drug_recalls.slice(0, 3).map((recall, i) => (
                          <div key={i} className="pulse-recall-item" {...activatable(() => openDetail(`recall-${i}`), `Open recall detail: ${recall.product}`)}>
                            <div className="pulse-recall-item__product">{recall.product}</div>
                            <div className="pulse-recall-item__reason">{recall.reason}</div>
                            {recall.classification && (
                              <span className="pulse-recall-item__class">{recall.classification}</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </>
                  )}
                </>
              ) : null}
            </div>

            {/* Footer */}
            <div className="pulse-panel__footer">
              <span className="pulse-panel__footer-text">
                Inbound only · Zero patient data transmitted
              </span>
              <button className="pulse-panel__footer-refresh" onClick={fetchPulse}>
                Refresh
              </button>
            </div>

            {/* Detail overlay */}
            {detailView && pulseData && (
              <PulseDetail
                view={detailView}
                data={{ ...pulseData, _selectedAlert: selectedAlert }}
                onBack={() => { setDetailView(null); setSelectedAlert(null) }}
              />
            )}
          </div>
        </>
      )}
    </>
  )
}
