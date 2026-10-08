import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  adoptMedOrders,
  cancelMedOrderDraft,
  createMedOrder,
  errorCode,
  fetchMedOrderEvents,
  fetchPatientMedOrders,
  fetchToSignQueue,
  isFeatureOff,
  MEDORDERS_UPDATE_EVENT,
  signMedOrder,
  subscribeMedOrderUpdates,
} from './medorders'

const apiFetch = vi.fn()
const socketOn = vi.fn()
const socketOff = vi.fn()

vi.mock('./client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  socket: {
    on: (event: string, fn: unknown) => socketOn(event, fn),
    off: (event: string, fn: unknown) => socketOff(event, fn),
  },
}))

beforeEach(() => {
  apiFetch.mockReset()
  socketOn.mockReset()
  socketOff.mockReset()
  apiFetch.mockResolvedValue({ order: { id: 'o1' }, events: [] })
})

describe('the medication-order client', () => {
  it('reads under /medorders and escapes ids in the path', async () => {
    await fetchPatientMedOrders('P/1')
    expect(apiFetch).toHaveBeenLastCalledWith('/medorders/patient/P%2F1', undefined)
    await fetchMedOrderEvents('o 1')
    expect(apiFetch).toHaveBeenLastCalledWith('/medorders/orders/o%201/events', undefined)
    await fetchToSignQueue()
    expect(apiFetch).toHaveBeenLastCalledWith('/medorders/queue/to-sign', undefined)
  })

  it('sends the Idempotency-Key header on create, only when given', async () => {
    await createMedOrder({ patient_id: 'P1', drug_name: 'Paracetamol', status: 'draft' }, 'k-1')
    let [path, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(path).toBe('/medorders/orders')
    expect(options.method).toBe('POST')
    expect(options.headers).toEqual({ 'Idempotency-Key': 'k-1' })
    expect(JSON.parse(String(options.body))).toEqual(
      { patient_id: 'P1', drug_name: 'Paracetamol', status: 'draft' })
    await createMedOrder({ patient_id: 'P1', drug_name: 'Paracetamol' })
    ;[, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(options.headers).toBeUndefined()
  })

  it('signs with the version on screen and the acknowledgement only when there is one', async () => {
    await signMedOrder('o1', 3)
    let [path, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(path).toBe('/medorders/orders/o1/sign')
    expect(JSON.parse(String(options.body))).toEqual({ expected_version: 3 })
    await signMedOrder('o1', 3, { acknowledged: true, reason: 'Desensitisation protocol' })
    ;[path, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(JSON.parse(String(options.body))).toEqual(
      { expected_version: 3, safety_ack: { acknowledged: true, reason: 'Desensitisation protocol' } })
  })

  it('cancels a draft with its reason', async () => {
    await cancelMedOrderDraft('o1', 'Wrong patient', 1)
    const [path, options] = apiFetch.mock.calls.at(-1) as [string, RequestInit]
    expect(path).toBe('/medorders/orders/o1/cancel')
    expect(JSON.parse(String(options.body))).toEqual({ reason: 'Wrong patient', expected_version: 1 })
  })

  it('adopts every row or only the named patients', async () => {
    await adoptMedOrders()
    expect(JSON.parse(String((apiFetch.mock.calls.at(-1) as [string, RequestInit])[1].body))).toEqual({})
    await adoptMedOrders(['P1'])
    expect(JSON.parse(String((apiFetch.mock.calls.at(-1) as [string, RequestInit])[1].body)))
      .toEqual({ patient_ids: ['P1'] })
  })

  it('recognises the switched-off answer and reads refusal codes', () => {
    expect(isFeatureOff({ status: 404, detail: { error: 'feature_disabled' } })).toBe(true)
    expect(isFeatureOff({ status: 404, detail: 'Medication order not found' })).toBe(false)
    expect(isFeatureOff(null)).toBe(false)
    expect(errorCode({ status: 409, detail: { error: 'screen_stale' } })).toBe('screen_stale')
    // A 403 reaches the caller with its body under `permissions` (apiFetch).
    expect(errorCode({ status: 403, permissions: { error: 'prescriber_required' } }))
      .toBe('prescriber_required')
    expect(errorCode(new Error('x'))).toBeNull()
  })

  it('subscribes to the ids-only update and unsubscribes the same handler', () => {
    const handler = vi.fn()
    const stop = subscribeMedOrderUpdates(handler)
    expect(MEDORDERS_UPDATE_EVENT).toBe('medorders:update')
    expect(socketOn).toHaveBeenCalledWith('medorders:update', handler)
    stop()
    expect(socketOff).toHaveBeenCalledWith('medorders:update', handler)
  })
})
