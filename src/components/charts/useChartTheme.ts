import { useEffect, useState } from 'react'

/**
 * Theme-resolved colours for the charting layer.
 *
 * recharts renders axes / gridlines / tooltips as SVG *presentation attributes*
 * (e.g. `<line stroke="…">`), which do NOT resolve CSS `var()`. So charts used to
 * hardcode `rgba(255,255,255,…)` white — invisible on the light and night
 * surfaces. This hook reads the `--chart-*` design tokens off the document root
 * with `getComputedStyle` and re-reads them whenever the theme flips, so charts
 * stay in lockstep with the active theme in one place instead of every call site.
 */
export interface ChartTheme {
  /** Axis lines + tick marks. */
  axis: string
  /** Background gridlines. */
  grid: string
  /** Tick label text. */
  tick: string
  /** Tooltip surface, text, and border. */
  tooltipBg: string
  tooltipText: string
  tooltipBorder: string
  /** Semantic status colours, resolved for series that encode severity. */
  statusOk: string
  statusWarn: string
  statusDanger: string
  statusInfo: string
  /** Opaque card surface — e.g. dot fills that must sit on the chart card. */
  surface: string
}

const FALLBACK: ChartTheme = {
  axis: 'rgba(245, 244, 238, 0.40)',
  grid: 'rgba(245, 244, 238, 0.09)',
  tick: '#c2c0b6',
  tooltipBg: '#33322e',
  tooltipText: '#f5f4ee',
  tooltipBorder: 'rgba(255, 255, 255, 0.14)',
  statusOk: '#66bb85',
  statusWarn: '#d9a84e',
  statusDanger: '#e8756a',
  statusInfo: '#6faedd',
  surface: '#2f2e2b',
}

function resolveChartTheme(): ChartTheme {
  if (typeof window === 'undefined' || typeof getComputedStyle !== 'function') {
    return FALLBACK
  }
  const cs = getComputedStyle(document.documentElement)
  const read = (name: string, fallback: string) => {
    const v = cs.getPropertyValue(name).trim()
    return v || fallback
  }
  return {
    axis: read('--chart-axis', FALLBACK.axis),
    grid: read('--chart-grid', FALLBACK.grid),
    tick: read('--chart-tick', FALLBACK.tick),
    tooltipBg: read('--chart-tooltip-bg', FALLBACK.tooltipBg),
    tooltipText: read('--chart-tooltip-text', FALLBACK.tooltipText),
    tooltipBorder: read('--chart-tooltip-border', FALLBACK.tooltipBorder),
    statusOk: read('--status-ok', FALLBACK.statusOk),
    statusWarn: read('--status-warn', FALLBACK.statusWarn),
    statusDanger: read('--status-danger', FALLBACK.statusDanger),
    statusInfo: read('--status-info', FALLBACK.statusInfo),
    surface: read('--bg-surface', FALLBACK.surface),
  }
}

export function useChartTheme(): ChartTheme {
  const [theme, setTheme] = useState<ChartTheme>(resolveChartTheme)

  useEffect(() => {
    const update = () => setTheme(resolveChartTheme())
    // The app dispatches 'theme-changed' on every theme switch (see ClinicianShell
    // / App). Also observe the data-theme attribute directly as a belt-and-braces
    // fallback for any switch path that doesn't dispatch the event.
    window.addEventListener('theme-changed', update)
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => {
      window.removeEventListener('theme-changed', update)
      observer.disconnect()
    }
  }, [])

  return theme
}
