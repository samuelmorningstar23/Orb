import { useState } from 'react'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import OfflinePatientPicker from './OfflinePatientPicker'
import { apiFetch } from '../api/client'
vi.mock('../api/client', () => ({apiFetch: vi.fn()}))
function deferred<T>() {let resolve!: (value: T) => void, reject!: (value: unknown) => void; const promise = new Promise<T>((res, rej) => {resolve = res; reject = rej}); return {promise, resolve, reject}}
const identity = (id: string) => ({id, name: `Synthetic ${id}`, mrn: `MRN-${id}`, status: 'admitted'})
function Picker() {const [value, setValue] = useState(''); return <OfflinePatientPicker value={value} onChange={setValue} />}
async function search(value: string) {fireEvent.change(screen.getByLabelText('Search name, hospital number or record ID'), {target: {value}}); await act(async () => {vi.advanceTimersByTime(250)})}
beforeEach(() => {vi.resetAllMocks(); vi.useFakeTimers()})
afterEach(() => {vi.useRealTimers()})

it('does not show results from an earlier patient search', async () => {
  const first = deferred<{patients: ReturnType<typeof identity>[]}>(), second = deferred<{patients: ReturnType<typeof identity>[]}>()
  vi.mocked(apiFetch).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
  render(<Picker />)
  await search('first'); await search('second')
  await act(async () => {second.resolve({patients: [identity('CURRENT')]}); await second.promise})
  expect(screen.getByRole('button', {name: 'Synthetic CURRENT · MRN-CURRENT'})).toBeInTheDocument()
  await act(async () => {first.resolve({patients: [identity('STALE')]}); await first.promise})
  expect(screen.queryByText(/Synthetic STALE/)).not.toBeInTheDocument()
})

it('clearing the search invalidates an in-flight result and its loading indicator', async () => {
  const pending = deferred<{patients: ReturnType<typeof identity>[]}>()
  vi.mocked(apiFetch).mockReturnValue(pending.promise)
  render(<Picker />)
  await search('patient')
  fireEvent.change(screen.getByLabelText('Search name, hospital number or record ID'), {target: {value: 'p'}})
  await act(async () => {pending.resolve({patients: [identity('STALE')]}); await pending.promise})
  expect(screen.queryByRole('status')).not.toBeInTheDocument()
  expect(screen.queryByText(/Synthetic STALE/)).not.toBeInTheDocument()
})

it('an authoritative refusal removes the held selected patient identity', async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce({patients: [identity('FIRST')]}).mockRejectedValueOnce(Object.assign(new Error('Access refused'), {status: 403}))
  render(<Picker />)
  await search('first')
  fireEvent.click(screen.getByRole('button', {name: 'Synthetic FIRST · MRN-FIRST'}))
  expect(screen.getByText(/Selected record: Synthetic FIRST/)).toBeInTheDocument()
  await search('second')
  await act(async () => {})
  expect(screen.getByRole('alert')).toHaveTextContent('Access refused')
  expect(screen.queryByText(/Selected record:/)).not.toBeInTheDocument()
  expect(screen.getByLabelText('Search name, hospital number or record ID')).toHaveValue('')
  expect(apiFetch).toHaveBeenCalledTimes(2)
})
