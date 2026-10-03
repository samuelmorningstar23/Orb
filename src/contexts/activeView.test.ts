import { describe, it, expect } from 'vitest'
import { buildWireViewContext } from './ActiveViewContext'

describe('buildWireViewContext', () => {
  it('returns undefined when nothing is on screen', () => {
    expect(buildWireViewContext('', null, null, null)).toBeUndefined()
  })

  it('carries the active tab', () => {
    expect(buildWireViewContext('orders', null, null, null)).toEqual({ tab: 'orders' })
  })

  it('carries the active patient (name + id)', () => {
    const wire = buildWireViewContext('vigil', 'P001', 'Kavya R', null)
    expect(wire).toMatchObject({ tab: 'vigil', patient_id: 'P001', patient_name: 'Kavya R' })
  })

  it('carries a panel selection, snake-casing the patient id', () => {
    const wire = buildWireViewContext('orders', null, null, {
      kind: 'order',
      id: 'ORD-9',
      label: 'Metoprolol 25mg',
      patientId: 'P002',
    })
    expect(wire).toMatchObject({
      tab: 'orders',
      selection: { kind: 'order', id: 'ORD-9', label: 'Metoprolol 25mg', patient_id: 'P002' },
    })
  })

  it('omits patient fields when there is no active patient', () => {
    const wire = buildWireViewContext('lens', null, null, null)!
    expect(wire.patient_id).toBeUndefined()
    expect(wire.patient_name).toBeUndefined()
  })
})
