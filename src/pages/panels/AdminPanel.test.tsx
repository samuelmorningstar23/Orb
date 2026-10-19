/**
 * THE ADMIN SUB-NAV IS A TAB LIST, NOT FIFTEEN ANONYMOUS BUTTONS.
 *
 * The outer pillar rail implements the WAI-ARIA tabs pattern in full. Its
 * fifteen admin sub-tabs, one level in on the same screen, were plain
 * <button>s in a <nav>: no role, no aria-selected, no aria-controls, no arrow
 * keys, and fifteen Tab stops. A screen-reader user heard fifteen unlabelled
 * buttons and was told which one was open by the pill fill alone — colour as
 * the only carrier of state.
 *
 * These pin the four halves of that contract (roles, selected state, a real
 * controlled panel, roving arrow-key focus) so the two navs cannot drift apart
 * again. The keyboard half is the SAME helper the rail uses (lib/tablist), so
 * a fix in one is a fix in both.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

/* Every sub-panel is a data-fetching screen of its own; this file is about the
   nav, so each one is stubbed down to a marker. Factories are inlined because
   vi.mock hoists above any local helper. */
vi.mock('./admin/AdminOverview', () => ({ default: () => <div>Overview content</div> }))
vi.mock('./admin/AuditLogTable', () => ({ default: () => <div>Audit content</div> }))
vi.mock('./admin/CorrelationTrace', () => ({ default: () => <div>Trace content</div> }))
vi.mock('./admin/IngestionMonitor', () => ({ default: () => <div>Ingestion content</div> }))
vi.mock('./admin/SecurityActivity', () => ({ default: () => <div>Security content</div> }))
vi.mock('./admin/AlertOperations', () => ({ default: () => <div>Alerts content</div> }))
vi.mock('./admin/ModelGovernance', () => ({ default: () => <div>Models content</div> }))
vi.mock('./admin/ImpactDashboard', () => ({ default: () => <div>Impact content</div> }))
vi.mock('./admin/Scorecard', () => ({ default: () => <div>Scorecard content</div> }))
vi.mock('./admin/TrustCenter', () => ({ default: () => <div>Trust content</div> }))
vi.mock('./admin/UsersPanel', () => ({ default: () => <div>Users content</div> }))
vi.mock('./admin/DataPanel', () => ({ default: () => <div>Data content</div> }))
vi.mock('./admin/RosterImport', () => ({ default: () => <div>Roster content</div> }))
vi.mock('./admin/ProvenanceReview', () => ({ default: () => <div>Provenance content</div> }))
vi.mock('./admin/FacilitiesPanel', () => ({ default: () => <div>Facilities content</div> }))
vi.mock('../../components/FlightRecorder', () => ({ default: () => <div>Flight content</div> }))

import AdminPanel from './AdminPanel'

const tablist = () => screen.getByRole('tablist', { name: /administration/i })
const tabs = () => within(tablist()).getAllByRole('tab')

describe('admin sub-tab semantics', () => {
  it('announces itself as a tab list of tabs, not a row of buttons', () => {
    render(<AdminPanel />)
    expect(tabs()).toHaveLength(16)
    // Nothing in the strip is left as a bare button.
    expect(within(tablist()).queryAllByRole('button')).toHaveLength(0)
  })

  it('states which tab is open in the accessibility tree, not only in colour', () => {
    render(<AdminPanel />)
    const overview = screen.getByRole('tab', { name: /Overview/ })
    const users = screen.getByRole('tab', { name: /^Users$/ })

    expect(overview).toHaveAttribute('aria-selected', 'true')
    expect(users).toHaveAttribute('aria-selected', 'false')
    expect(tabs().filter(t => t.getAttribute('aria-selected') === 'true')).toHaveLength(1)

    fireEvent.click(users)
    expect(users).toHaveAttribute('aria-selected', 'true')
    expect(overview).toHaveAttribute('aria-selected', 'false')
    expect(screen.getByText('Users content')).toBeInTheDocument()
  })

  it('points every tab at a panel that actually exists', () => {
    render(<AdminPanel />)
    const panel = screen.getByRole('tabpanel')

    for (const tab of tabs()) {
      const controls = tab.getAttribute('aria-controls')
      expect(controls, `${tab.textContent} has no aria-controls`).toBeTruthy()
      // A dangling IDREF is worse than none: the reference must resolve.
      expect(document.getElementById(controls!)).toBe(panel)
    }
    expect(panel).toHaveAttribute('aria-labelledby', 'admin-tab-overview')

    fireEvent.click(screen.getByRole('tab', { name: /Trust Center/ }))
    expect(screen.getByRole('tabpanel')).toHaveAttribute('aria-labelledby', 'admin-tab-trust')
  })

  it('holds exactly one Tab stop — the open tab', () => {
    render(<AdminPanel />)
    const stops = tabs().filter(t => t.getAttribute('tabindex') === '0')
    expect(stops).toHaveLength(1)
    expect(stops[0]).toBe(screen.getByRole('tab', { name: /Overview/ }))

    fireEvent.click(screen.getByRole('tab', { name: /Alerts/ }))
    const moved = tabs().filter(t => t.getAttribute('tabindex') === '0')
    expect(moved).toHaveLength(1)
    expect(moved[0]).toBe(screen.getByRole('tab', { name: /Alerts/ }))
  })

  it('moves between tabs on the arrow keys, and wraps with Home/End', () => {
    render(<AdminPanel />)
    const list = tablist()
    const all = tabs()

    fireEvent.keyDown(list, { key: 'ArrowRight' })
    expect(all[1]).toHaveAttribute('aria-selected', 'true')
    expect(all[1]).toHaveFocus()
    expect(screen.getByText('Scorecard content')).toBeInTheDocument()

    fireEvent.keyDown(list, { key: 'ArrowLeft' })
    expect(all[0]).toHaveAttribute('aria-selected', 'true')
    expect(all[0]).toHaveFocus()

    // ArrowLeft off the first tab wraps to the last, as the rail does.
    fireEvent.keyDown(list, { key: 'ArrowLeft' })
    expect(all[all.length - 1]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(list, { key: 'Home' })
    expect(all[0]).toHaveAttribute('aria-selected', 'true')

    fireEvent.keyDown(list, { key: 'End' })
    expect(all[all.length - 1]).toHaveAttribute('aria-selected', 'true')
    expect(all[all.length - 1]).toHaveFocus()
  })

  it('opens the facility identity screen from its own tab, next to Data', () => {
    render(<AdminPanel />)
    const names = tabs().map(t => t.textContent)
    expect(names.indexOf('Facilities')).toBe(names.indexOf('Data & Backups') - 1)
    fireEvent.click(screen.getByRole('tab', { name: 'Facilities' }))
    expect(screen.getByText('Facilities content')).toBeInTheDocument()
  })

  it('leaves keys it does not own to the browser', () => {
    render(<AdminPanel />)
    const list = tablist()
    fireEvent.keyDown(list, { key: 'Tab' })
    expect(tabs()[0]).toHaveAttribute('aria-selected', 'true')
  })
})
