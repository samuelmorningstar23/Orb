/**
 * The Sage medication order card sends a real pharmacy order.
 *
 * It used to send '' for whatever dose, route or frequency the model left out,
 * dropped the server's refusal (and the 428 safety payload) into the console,
 * and named the internal module on its button. These tests pin the card that
 * replaced it: missing fields are typed in, a severe alert asks for a reason,
 * and a refusal is shown in the server's own words.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const approveHelixOrder = vi.fn();

vi.mock('../../api/client', () => ({
  approveHelixOrder: (...args: unknown[]) => approveHelixOrder(...args),
  isSafetyAckRequired: (e: unknown) => !!e && (e as { code?: string }).code === 'safety_ack_required',
  // The signed-in pharmacist: the role decides who may clear an allergy block.
  getUser: () => ({ id: 'PHA001', name: 'Priya Nair', role: 'pharmacist' }),
}));

import { SageMessage } from './SageMessage';

function renderCard(payload: Record<string, string>) {
  return render(
    <SageMessage
      message={{
        role: 'assistant',
        content: 'Drafted request for Enoxaparin.',
        metadata: {
          action_state: {
            action_type: 'add_medication',
            target_patient: 'Asha Rao',
            requested_payload: { action_type: 'add_medication', ...payload },
            status_code: 'request_drafted',
            safe_detail_message: 'Action available but requires explicit clinician approval before sending to Helix.',
          },
        },
      }}
      isStreaming={false}
      isSpeaking={false}
    />,
  );
}

describe('Sage medication order card', () => {
  beforeEach(() => { approveHelixOrder.mockReset() });

  it('collects what the draft left out before it can send', async () => {
    approveHelixOrder.mockResolvedValue({ status: 'success', order: {}, safety: { allergy_alerts: [], interaction_alerts: [] } });
    renderCard({ medication: 'Enoxaparin', dose: '40 mg' });

    const send = screen.getByRole('button', { name: 'Send to pharmacy' });
    expect(send).toBeDisabled();
    expect(screen.getByText('Enter the route and frequency before sending.')).toBeInTheDocument();
    expect(screen.queryByText(/Helix/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Route for Enoxaparin'), { target: { value: 'SC' } });
    fireEvent.change(screen.getByLabelText('Frequency for Enoxaparin'), { target: { value: 'OD' } });
    expect(send).toBeEnabled();
    fireEvent.click(send);

    await waitFor(() => expect(screen.getByText('Sent to Pharmacy')).toBeInTheDocument());
    expect(approveHelixOrder).toHaveBeenCalledWith({
      patient_name: 'Asha Rao', medication: 'Enoxaparin', dose: '40 mg', route: 'SC', frequency: 'OD',
    });
  });

  it('a 428 opens the reason box and re-sends with safety_ack', async () => {
    const ackErr = Object.assign(
      new Error('Severe drug-safety alerts must be acknowledged with a reason before this order proceeds.'),
      {
        code: 'safety_ack_required',
        status: 428,
        safety: {
          allergy_alerts: [{ severity: 'severe', message: 'Patient is allergic to Heparins.' }],
          interaction_alerts: [],
        },
      },
    );
    approveHelixOrder.mockRejectedValueOnce(ackErr).mockResolvedValueOnce({ status: 'success', order: {} });
    renderCard({ medication: 'Enoxaparin', dose: '40 mg', route: 'SC', frequency: 'OD' });

    fireEvent.click(screen.getByRole('button', { name: 'Send to pharmacy' }));
    expect(await screen.findByText('Patient is allergic to Heparins.')).toBeInTheDocument();

    const confirm = screen.getByRole('button', { name: 'Acknowledge and send' });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Reason for sending this order despite the alerts'), {
      target: { value: 'Allergy reviewed with haematology' },
    });
    fireEvent.click(confirm);

    await waitFor(() => expect(screen.getByText('Sent to Pharmacy')).toBeInTheDocument());
    expect(approveHelixOrder).toHaveBeenLastCalledWith(expect.objectContaining({
      safety_ack: { acknowledged: true, reason: 'Allergy reviewed with haematology' },
    }));
  });

  it('offers no reason box on a severe allergy to someone who cannot prescribe', async () => {
    const refusal = 'Only a prescriber can override an allergy block. Choose another medicine, or ask the prescriber.'
    const ackErr = Object.assign(new Error(refusal), {
      code: 'safety_ack_required',
      status: 428,
      safety: {
        allergy_alerts: [{ severity: 'severe', message: 'Patient is allergic to Heparins.' }],
        interaction_alerts: [],
        override_requires_permission: 'medication.prescribe',
        ack_refused: refusal,
      },
    });
    approveHelixOrder.mockRejectedValueOnce(ackErr);
    renderCard({ medication: 'Enoxaparin', dose: '40 mg', route: 'SC', frequency: 'OD' });

    fireEvent.click(screen.getByRole('button', { name: 'Send to pharmacy' }));
    expect(await screen.findByText('Patient is allergic to Heparins.')).toBeInTheDocument();
    expect(screen.getByText('Only a prescriber can override an allergy block.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Reason for sending this order despite the alerts')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Acknowledge and send' })).toBeNull();
    // The refusal is the heading, and is not listed a second time.
    expect(screen.getAllByText(refusal)).toHaveLength(1);
  });

  it('shows a refusal in the server\'s words and keeps the order sendable', async () => {
    const refusal = Object.assign(new Error('Name \'Asha Rao\' matched 2 patients.'), { status: 422 });
    approveHelixOrder.mockRejectedValueOnce(refusal);
    renderCard({ medication: 'Enoxaparin', dose: '40 mg', route: 'SC', frequency: 'OD' });

    fireEvent.click(screen.getByRole('button', { name: 'Send to pharmacy' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('matched 2 patients');
    expect(screen.getByRole('button', { name: 'Send to pharmacy' })).toBeEnabled();
    expect(screen.queryByText('Sent to Pharmacy')).not.toBeInTheDocument();
    expect(approveHelixOrder).toHaveBeenCalledTimes(1);
  });
});
