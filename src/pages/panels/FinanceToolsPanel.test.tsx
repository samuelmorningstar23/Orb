import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import FinanceToolsPanel from './FinanceToolsPanel'
import * as api from '../../api/financeTools'
import type { FinanceMeta, JournalPreview, PayoutPreview, PayoutStatement } from '../../api/financeTools'

vi.mock('../../api/financeTools', () => ({
  approvePayoutStatement: vi.fn(), attributeFinanceCharge: vi.fn(), cancelPayoutStatement: vi.fn(), createJournalExport: vi.fn(), createPayoutRule: vi.fn(), createPayoutStatement: vi.fn(), downloadJournal: vi.fn(), downloadPayoutStatement: vi.fn(), getFinanceCharges: vi.fn(), getFinanceMeta: vi.fn(), getJournalExports: vi.fn(), getPayoutStatement: vi.fn(), getPayoutStatements: vi.fn(), markPayoutPaid: vi.fn(), previewJournal: vi.fn(), previewPayout: vi.fn(), retirePayoutRule: vi.fn(), saveAccountMapping: vi.fn(),
}))
const meta: FinanceMeta = { facility_id: 1, account_roles: [{ id: 'receivable', label: 'Receivable' }], mappings: [], doctors: [{ id: 'SYN-DOC', name: 'Synthetic Doctor' }], rules: [], limits: { days: 366, sources: 2000, lines: 10000 }, notes: ['No account or share rule is supplied.'] }
const journal: JournalPreview = { facility_id: 1, from_day: '2026-10-01', to_day: '2026-10-10', rows: [], issues: [{ code: 'account_not_mapped', account_role: 'receivable' }], debit_paise: 10000, credit_paise: 10000, balanced: true, can_export: false, mappings: {} }
const payout: PayoutPreview = { facility_id: 1, from_day: '2026-10-01', to_day: '2026-10-10', doctor_id: null, lines: [], issues: [{ code: 'doctor_not_attributed', source_line_id: 123 }], excluded: [], total_paise: 0, preview_hash: 'hash', can_create: false }
const statement: PayoutStatement = { facility_id: 1, from_day: '2026-10-01', to_day: '2026-10-10', id: 'synthetic-statement', doctor_id: 'SYN-DOC', status: 'approved', total_paise: 25000, deduction_paise: 500, net_paise: 24500, deduction_reason: 'Explicit synthetic deduction', preview_hash: 'hash', created_by: 'SYN-CREATOR', created_at: '2026-10-11', approved_by: 'SYN-APPROVER', approved_at: '2026-10-11', paid_by: null, paid_at: null, payment_reference: null, cancelled_by: null, cancelled_at: null, lines: [] }
async function ready() { render(<FinanceToolsPanel />); await screen.findByRole('button', { name: 'Accounting journal' }) }
function dates() { fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-10-01' } }); fireEvent.change(screen.getByLabelText('Through date'), { target: { value: '2026-10-10' } }) }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api.getFinanceMeta).mockResolvedValue(meta); vi.mocked(api.getPayoutStatements).mockResolvedValue([]); vi.mocked(api.previewJournal).mockResolvedValue(journal); vi.mocked(api.previewPayout).mockResolvedValue(payout) })
afterEach(cleanup)

describe('Accounting & doctor payouts', () => {
  it('reports an unavailable module without inventing zero earnings', async () => {
    vi.mocked(api.getFinanceMeta).mockRejectedValue(new Error('Finance tools are disabled.'))
    render(<FinanceToolsPanel />)
    expect(await screen.findByRole('alert')).toHaveTextContent('disabled')
    expect(screen.queryByText('Total ₹0.00')).not.toBeInTheDocument()
  })
  it('requires account mappings before journal export', async () => {
    await ready(); dates(); fireEvent.click(screen.getByRole('button', { name: 'Preview journal' }))
    expect(await screen.findByText(/Map these accounts before export/)).toHaveTextContent('receivable')
    expect(screen.getByRole('button', { name: 'Create snapshot & export CSV' })).toBeDisabled()
  })
  it('clears old journal records when the period changes', async () => {
    await ready(); dates(); fireEvent.click(screen.getByRole('button', { name: 'Preview journal' })); await screen.findByText('Balanced source journal')
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-10-02' } })
    expect(screen.queryByText('Balanced source journal')).not.toBeInTheDocument()
  })
  it('never preselects an earning doctor from the charge author', async () => {
    vi.mocked(api.getFinanceCharges).mockResolvedValue({ charges: [{ id: 1, patient_id: 'SYN-P', patient_name: 'Synthetic Patient', service_code: 'CONSULT', description: 'Synthetic consultation', status: 'billed', service_date: null, gross_paise: 10000, discount_paise: 0, total_paise: 10000, invoice_id: 1, attribution: null }] })
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Doctor attribution' })); dates(); fireEvent.click(screen.getByRole('button', { name: 'Load charge worklist' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Attribute charge 1' }))
    expect(screen.getByLabelText('Earning doctor')).toHaveValue('')
    expect(screen.getByLabelText('Attribution evidence or reason')).toHaveValue('')
  })
  it('sends explicit basis and recognition and parses percentages to integer basis points', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Payout rules' }))
    expect(screen.getByLabelText('Amount method')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('Doctor'), { target: { value: 'SYN-DOC' } })
    fireEvent.change(screen.getByLabelText('Applies to'), { target: { value: 'all' } })
    fireEvent.change(screen.getByLabelText('Amount method'), { target: { value: 'percentage' } })
    fireEvent.change(screen.getByLabelText('Basis'), { target: { value: 'net_ex_tax' } })
    fireEvent.change(screen.getByLabelText('Recognition'), { target: { value: 'fully_collected' } })
    fireEvent.change(screen.getByLabelText('Share percentage (0–100)'), { target: { value: '25.10' } })
    fireEvent.change(screen.getByLabelText('Effective from'), { target: { value: '2026-10-01' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create rule' }))
    await waitFor(() => expect(api.createPayoutRule).toHaveBeenCalledWith({ facility_id: 1, doctor_id: 'SYN-DOC', selector_type: 'all', selector_value: '', mode: 'percentage', basis: 'net_ex_tax', recognition: 'fully_collected', percent_bp: 2510, effective_from: '2026-10-01' }))
  })
  it('shows missing attribution and disables statement creation', async () => {
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Payout statements' })); dates(); fireEvent.click(screen.getByRole('button', { name: 'Preview earnings' }))
    expect(await screen.findByText(/doctor not attributed/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create draft statement' })).toBeDisabled()
  })
  it('records a payment reference explicitly, without claiming funds were sent', async () => {
    vi.mocked(api.getPayoutStatements).mockResolvedValue([statement]); vi.mocked(api.getPayoutStatement).mockResolvedValue(statement)
    vi.mocked(api.markPayoutPaid).mockResolvedValue({ ...statement, status: 'paid', payment_reference: 'EXTERNAL-REF' })
    await ready(); fireEvent.click(screen.getByRole('button', { name: 'Payout statements' })); fireEvent.click(screen.getByRole('button', { name: 'Open syntheti' }))
    await screen.findByLabelText('Reference for payment already made')
    fireEvent.change(screen.getByLabelText('Reference for payment already made'), { target: { value: 'EXTERNAL-REF' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record payment already made' }))
    await waitFor(() => expect(api.markPayoutPaid).toHaveBeenCalledWith('synthetic-statement', 'EXTERNAL-REF'))
    expect(await screen.findByText(/No funds were transmitted/)).toBeInTheDocument()
  })
})
