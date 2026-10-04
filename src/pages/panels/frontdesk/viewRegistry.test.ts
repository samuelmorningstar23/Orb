import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  DEFAULT_VIEW_ORDER,
  discoverFrontDeskViews,
  labelFromId,
  viewIdFromPath,
  visibleFrontDeskViews,
} from './viewRegistry'

const A = () => null
const B = () => null

describe('front desk view registry', () => {
  afterEach(() => vi.restoreAllMocks())

  it('derives the id and the sentence-case label from the file name', () => {
    expect(viewIdFromPath('./frontdesk/ClinicSessionsView.tsx')).toBe('clinic-sessions')
    expect(viewIdFromPath('./frontdesk/AppointmentsDayView.tsx')).toBe('appointments-day')
    expect(viewIdFromPath('./frontdesk/viewRegistry.ts')).toBeNull()
    expect(labelFromId('clinic-sessions')).toBe('Clinic sessions')
  })

  it('orders by viewOrder, then label, and keeps declared labels and permissions', () => {
    const views = discoverFrontDeskViews({
      './frontdesk/ZetaView.tsx': { default: A },
      './frontdesk/ClinicSessionsView.tsx': {
        default: B, viewLabel: 'Clinic timings', viewOrder: 30, viewPermission: 'registration.read',
      },
    })
    expect(views.map(v => [v.id, v.label, v.order, v.permission])).toEqual([
      ['clinic-sessions', 'Clinic timings', 30, 'registration.read'],
      ['zeta', 'Zeta', DEFAULT_VIEW_ORDER, null],
    ])
  })

  it('leaves out a file with no component, a bad id, a repeated id or a built-in id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const views = discoverFrontDeskViews({
      './frontdesk/NoComponentView.tsx': { viewId: 'x' },
      './frontdesk/BadIdView.tsx': { default: A, viewId: 'Bad Id' },
      './frontdesk/OneView.tsx': { default: A, viewId: 'same' },
      './frontdesk/TwoView.tsx': { default: B, viewId: 'same' },
      './frontdesk/QueueView.tsx': { default: A, viewId: 'opd' },
      './frontdesk/MovesView.tsx': { default: A, viewId: 'transfers' },
    })
    expect(views.map(v => v.id)).toEqual(['same'])
    expect(views[0].source).toBe('./frontdesk/OneView.tsx')
    expect(warn).toHaveBeenCalledTimes(5)
  })

  it('hides a view whose permission is not held, and never adds one', () => {
    const views = discoverFrontDeskViews({
      './frontdesk/OpenView.tsx': { default: A },
      './frontdesk/ManageView.tsx': { default: B, viewPermission: 'schedule.manage' },
    })
    expect(visibleFrontDeskViews(views, () => false).map(v => v.id)).toEqual(['open'])
    expect(visibleFrontDeskViews(views, p => p === 'schedule.manage').map(v => v.id).sort())
      .toEqual(['manage', 'open'])
  })

  it('finds the clinic timings view among the installed files', () => {
    const installed = discoverFrontDeskViews(
      import.meta.glob('./*View.tsx', { eager: true }),
    )
    const clinic = installed.find(v => v.id === 'clinic-sessions')
    expect(clinic?.label).toBe('Clinic timings')
    expect(clinic?.permission).toBe('registration.read')
  })
})
