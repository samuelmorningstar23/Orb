/**
 * Admin under the Orb Ward Record profile (6 October 2026): only the sections
 * Ward Record sells, Data first (it holds the ward intervals and the monthly
 * rounds file), then Users, the audit log and its trace, the Flight Recorder
 * and the Trust Center. An ordinary site keeps every section.
 */
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { resetClinicalSwitches, setClinicalSwitches } from '../../shellConfig'

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

const sections = () => screen.getAllByRole('tab').map(t => t.textContent)

afterEach(() => { cleanup(); resetClinicalSwitches() })

describe('Admin under the Ward Record profile', () => {
  it('lists only the Ward Record sections and opens on Data', () => {
    act(() => { setClinicalSwitches({ ward_record: true, scoring_enabled: false, models_enabled: false }) })
    render(<AdminPanel />)
    expect(sections()).toEqual(['Data & Backups', 'Users', 'Audit Logs', 'Trace', 'Flight Recorder', 'Trust Center'])
    expect(screen.getByText('Data content')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'Flight Recorder' }))
    expect(screen.getByText('Flight content')).toBeTruthy()
  })

  it('an ordinary site keeps every section and opens on the overview', () => {
    render(<AdminPanel />)
    expect(sections()).toHaveLength(16)
    expect(screen.getByText('Overview content')).toBeTruthy()
  })
})
