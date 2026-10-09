/**
 * The ward stock client (grn-21): every call goes to /pharmacy-store/ward/...
 * through the shared base, with the body the server's routes read.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const apiFetch = vi.fn()

vi.mock('../client', () => ({
  apiFetch: (path: string, options?: RequestInit) => apiFetch(path, options),
  buildHeaders: () => ({}),
  API_BASE: '/api',
  errorDetailMessage: (_d: unknown, status: number) => `Request failed (${status})`,
  handleResponseError: async () => { throw new Error('x') },
}))

import {
  WARD_INDENT_STATUS_LABELS,
  cancelWardIndent,
  decideWardIndent,
  getWardIndent,
  getWardIssuePlan,
  getWardStock,
  getWardTransfersAwaiting,
  issueWardIndent,
  listWardIndents,
  newIssueRequestKey,
  raiseWardIndent,
  receiveWardTransfer,
} from './ward'

function call(n = 0): [string, RequestInit | undefined] {
  return apiFetch.mock.calls[n] as [string, RequestInit | undefined]
}

function sentBody(n = 0) {
  return JSON.parse(String(call(n)[1]?.body))
}

beforeEach(() => {
  apiFetch.mockReset()
  apiFetch.mockResolvedValue({})
})

afterEach(() => vi.clearAllMocks())

describe('ward stock client', () => {
  it('reads the stock, the transfers on their way and the indents', async () => {
    await getWardStock()
    await getWardStock('WS-CARD')
    await getWardTransfersAwaiting()
    await listWardIndents({ status: 'open' })
    await getWardIndent(12)
    await getWardIssuePlan(12)
    expect(apiFetch.mock.calls.map(c => c[0])).toEqual([
      '/pharmacy-store/ward/stock',
      '/pharmacy-store/ward/stock?store_code=WS-CARD',
      '/pharmacy-store/ward/transfers/awaiting',
      '/pharmacy-store/ward/indents?status=open',
      '/pharmacy-store/ward/indents/12',
      '/pharmacy-store/ward/indents/12/issue-plan',
    ])
  })

  it('posts a raise, a decision, a cancel, an issue and a ward receipt', async () => {
    await raiseWardIndent({ store_code: 'WS-CARD',
      lines: [{ helix_medication_id: 'MED-PANTO', quantity_units: 14 }] })
    await decideWardIndent(12, { approvals: [{ line_no: 1, units: 10 }] })
    await cancelWardIndent(12, 'Asked twice')
    await issueWardIndent(12, { idempotency_key: 'k1' })
    await receiveWardTransfer(41, [{ line_id: 501, received_units: 10 }])
    expect(apiFetch.mock.calls.map(c => [c[0], (c[1] as RequestInit).method])).toEqual([
      ['/pharmacy-store/ward/indents', 'POST'],
      ['/pharmacy-store/ward/indents/12/decision', 'POST'],
      ['/pharmacy-store/ward/indents/12/cancel', 'POST'],
      ['/pharmacy-store/ward/indents/12/issue', 'POST'],
      ['/pharmacy-store/ward/transfers/41/receive', 'POST'],
    ])
    expect(sentBody(2)).toEqual({ reason: 'Asked twice' })
    expect(sentBody(4)).toEqual({ lines: [{ line_id: 501, received_units: 10 }] })
  })

  it('sends the witness with a ward receipt only when one is given', async () => {
    await receiveWardTransfer(41, [{ line_id: 501, received_units: 5 }],
      { witness_id: 'PHA002', witness_pin: '4826' })
    await receiveWardTransfer(42, [{ line_id: 502, received_units: 5 }], null)
    expect(sentBody(0)).toEqual({ lines: [{ line_id: 501, received_units: 5 }],
      witness_id: 'PHA002', witness_pin: '4826' })
    expect(sentBody(1)).toEqual({ lines: [{ line_id: 502, received_units: 5 }] })
  })

  it('names every indent status in plain words and makes fresh issue keys', () => {
    expect(WARD_INDENT_STATUS_LABELS.converted).toBe('Sent to the ward')
    expect(WARD_INDENT_STATUS_LABELS.closed).toBe('Received')
    const a = newIssueRequestKey()
    expect(a).toMatch(/^ward-issue-/)
    expect(newIssueRequestKey()).not.toBe(a)
  })
})
