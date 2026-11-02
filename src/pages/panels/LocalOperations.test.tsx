import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FeedbackPanel from './FeedbackPanel'
import MortuaryPanel from './MortuaryPanel'
import CssdPanel from './CssdPanel'
import { useOperations } from '../../hooks/useLocalOperations'
import * as feedback from '../../api/feedback'
import * as mortuary from '../../api/mortuary'
import * as cssd from '../../api/cssd'

vi.mock('../../api/client', () => ({ getUser: () => ({ id: 'TEST-ADMIN', role: 'admin' }) }))
vi.mock('../../components/OfflinePatientPicker', () => ({ default: () => <p>Scoped patient picker</p> }))
vi.mock('../../api/feedback', () => ({ loadFeedback: vi.fn(), getFeedback: vi.fn(), recordFeedback: vi.fn(), actOnFeedback: vi.fn() }))
vi.mock('../../api/mortuary', () => ({ loadMortuary: vi.fn(), getMortuaryCase: vi.fn(), addMortuarySlot: vi.fn(), recordMortuaryIntake: vi.fn(), actOnMortuaryCase: vi.fn() }))
vi.mock('../../api/cssd', () => ({ loadCssd: vi.fn(), getCssdPack: vi.fn(), getCssdCycle: vi.fn(), createCssdSet: vi.fn(), createCssdPack: vi.fn(), startCssdCycle: vi.fn(), actOnCssdPack: vi.fn(), actOnCssdCycle: vi.fn() }))

function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(res => { resolve = res }); return { promise, resolve } }
const record = { id: 'FB-1', patient_id: null, kind: 'complaint' as const, channel: 'written', subject: 'Synthetic complaint', statement: 'Original unedited statement', reporter_name: null, contact_reference: null, rating: null, owner_id: null, status: 'recorded', resolution: null, created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-01T00:00:00Z', version: 3, events: [] }
const base = { limit: 100, offset: 0, has_more: false, notice: 'Local only' }
const pack = { id: 'PACK-1', tag: 'PACK-TAG', set_id: 'SET-1', status: 'released', cycle_id: 'CYCLE-1', expires_at: '2000-01-01T00:00:00Z', version: 1, available_to_issue: false, events: [], issues: [] }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(feedback.loadFeedback).mockResolvedValue({ ...base, cases: [record] })
  vi.mocked(feedback.getFeedback).mockResolvedValue(record)
  vi.mocked(mortuary.loadMortuary).mockResolvedValue({ ...base, cases: [], slots: [] })
  vi.mocked(cssd.loadCssd).mockResolvedValue({ ...base, sets: [], packs: [pack], cycles: [] })
  vi.mocked(cssd.getCssdPack).mockResolvedValue(pack)
})

describe('local register request ownership', () => {
  it('ignores an older detail after another record is selected', async () => {
    const first = deferred<{ id: string }>(), second = deferred<{ id: string }>()
    const getter = vi.fn((id: string) => id === 'first' ? first.promise : second.promise), loader = vi.fn(async () => [])
    const { result } = renderHook(() => useOperations(loader, getter))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => { void result.current.select('first'); void result.current.select('second') })
    await act(async () => { second.resolve({ id: 'second' }); await second.promise })
    await act(async () => { first.resolve({ id: 'first' }); await first.promise })
    expect(result.current.selected?.id).toBe('second')
  })

  it('clears previous data and details on authoritative register refusal', async () => {
    const loader = vi.fn().mockResolvedValueOnce(['visible']).mockRejectedValueOnce(Object.assign(new Error('Access refused'), { status: 403 }))
    const getter = vi.fn(async () => ({ id: 'first' }))
    const { result } = renderHook(() => useOperations(loader, getter))
    await waitFor(() => expect(result.current.data).toEqual(['visible']))
    await act(async () => { await result.current.select('first') })
    await act(async () => { await result.current.refresh() })
    expect(result.current.data).toBeNull(); expect(result.current.selected).toBeNull()
    expect(result.current.error).toBe('Access refused')
  })

  it('locks duplicate mutation calls before React renders the busy state', async () => {
    const pending = deferred<void>(), operation = vi.fn(() => pending.promise), loader = vi.fn(async () => []), getter = vi.fn(async () => ({ id: 'first' }))
    const { result } = renderHook(() => useOperations(loader, getter))
    await waitFor(() => expect(result.current.loading).toBe(false))
    act(() => { void result.current.run(operation); void result.current.run(operation) })
    expect(operation).toHaveBeenCalledTimes(1)
    await act(async () => { pending.resolve(); await pending.promise })
    await waitFor(() => expect(result.current.busy).toBe(false))
  })

  it('loads later pages without retaining the prior selected record', async () => {
    const loader = vi.fn(async () => []), getter = vi.fn(async () => ({ id: 'first' }))
    const { result } = renderHook(() => useOperations(loader, getter))
    await waitFor(() => expect(result.current.loading).toBe(false))
    await act(async () => { await result.current.select('first') })
    act(() => result.current.page(100))
    await waitFor(() => expect(loader).toHaveBeenLastCalledWith(100))
    expect(result.current.selected).toBeNull()
  })
})

it('feedback preserves the draft and idempotency key after a lost response, and changes the key when edited', async () => {
  vi.mocked(feedback.recordFeedback).mockRejectedValue(new Error('Connection lost'))
  render(<FeedbackPanel />)
  fireEvent.click(screen.getByText('Record feedback or a complaint'))
  fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Synthetic draft' } })
  fireEvent.change(screen.getByLabelText('Original statement'), { target: { value: 'Draft original statement' } })
  const submit = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Save local record' }))
    await screen.findByRole('alert')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save local record' })).not.toBeDisabled())
  }
  await submit()
  expect(screen.getByLabelText('Original statement')).toHaveValue('Draft original statement')
  await submit()
  expect(vi.mocked(feedback.recordFeedback).mock.calls[1][1]).toBe(vi.mocked(feedback.recordFeedback).mock.calls[0][1])
  fireEvent.change(screen.getByLabelText('Original statement'), { target: { value: 'Edited original statement' } })
  await submit()
  expect(vi.mocked(feedback.recordFeedback).mock.calls[2][1]).not.toBe(vi.mocked(feedback.recordFeedback).mock.calls[0][1])
})

it('feedback sends the selected revision and authored response for resolution', async () => {
  vi.mocked(feedback.actOnFeedback).mockResolvedValue(record)
  render(<FeedbackPanel />)
  fireEvent.click(await screen.findByRole('button', { name: 'Synthetic complaint · recorded' }))
  await screen.findByText('Original unedited statement')
  fireEvent.change(screen.getByLabelText('Case action'), { target: { value: 'resolve' } })
  fireEvent.change(screen.getByLabelText('Response or reason'), { target: { value: 'Local response recorded' } })
  fireEvent.click(screen.getByRole('button', { name: 'Record action' }))
  await waitFor(() => expect(feedback.actOnFeedback).toHaveBeenCalledWith('FB-1', { action: 'resolve', version: 3, note: 'Local response recorded' }))
})

it('held mortuary custody offers clearance and movement but no handover', async () => {
  const held = { id: 'MT-1', patient_id: null, body_tag: 'BODY-TAG', identity_label: 'Synthetic identity', authority_reference: 'Paper TEST', received_at: '2026-10-01T00:00:00Z', slot_id: 'SLOT-1', status: 'held', hold_reason: 'Existing hold', version: 2, release_authority_reference: null, recipient_name: null, recipient_relationship: null, identity_check_reference: null, witness_id: null, released_at: null, events: [] }
  vi.mocked(mortuary.loadMortuary).mockResolvedValue({ ...base, cases: [held], slots: [{ id: 'SLOT-1', code: 'A1', name: 'Synthetic position', active: 1, occupied: true }] })
  vi.mocked(mortuary.getMortuaryCase).mockResolvedValue(held)
  render(<MortuaryPanel />)
  fireEvent.click(await screen.findByRole('button', { name: /BODY-TAG/ }))
  const actions = await screen.findByLabelText('Custody action')
  expect(actions.querySelector('option[value="release"]')).toBeNull()
  fireEvent.change(actions, { target: { value: 'clear_hold' } })
  expect(screen.getByLabelText('Existing clearance / release authority reference')).toBeRequired()
})

it('CSSD expiry prevents issue while retaining the reprocessing workflow', async () => {
  render(<CssdPanel />)
  fireEvent.click(await screen.findByRole('button', { name: 'PACK-TAG · released' }))
  const actions = await screen.findByLabelText('Pack action')
  expect(actions.querySelector('option[value="issue"]')).toBeNull()
  expect(actions.querySelector('option[value="reprocess"]')).not.toBeNull()
  expect(screen.getByText(/Unavailable for issue/)).toBeInTheDocument()
})

it('CSSD rejects fractional instrument quantities without calling the API', async () => {
  render(<CssdPanel />)
  fireEvent.click(screen.getByText('Define an instrument set'))
  fireEvent.change(screen.getByLabelText('Set code'), { target: { value: 'SET-TEST' } })
  fireEvent.change(screen.getByLabelText('Set name'), { target: { value: 'Synthetic set' } })
  fireEvent.change(screen.getByLabelText(/Instrument contents/), { target: { value: 'Forceps | 1.5' } })
  fireEvent.click(screen.getByRole('button', { name: 'Save set definition' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('whole-number quantity')
  expect(cssd.createCssdSet).not.toHaveBeenCalled()
})
