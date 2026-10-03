/**
 * What a clinician is allowed to see in a Sage answer bubble.
 *
 * The fast lane opens every reply with `NOACT|` (or `ACT|{intent}`). That is
 * contract syntax between the model and the server — never clinical content.
 * The streaming lane strips it server-side, but a turn reopened from
 * /sage/history arrives verbatim from the DB, because the raw model output is
 * kept as the provenance record. That lane leaked the marker into the bubble:
 * "NOACT| National Early Warning Score version 2…". These tests pin the strip
 * at the render layer, which every lane passes through.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { SageMessage } from './SageMessage';

function renderAssistant(content: string) {
  return render(
    <SageMessage
      message={{ role: 'assistant', content }}
      isStreaming={false}
      isSpeaking={false}
    />
  );
}

describe('fast-contract markers never reach the clinician', () => {
  it('strips a leading NOACT| from a reloaded turn', () => {
    renderAssistant('NOACT| National Early Warning Score version 2.');
    expect(screen.getByText(/National Early Warning Score version 2\./)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('NOACT|');
  });

  it('strips ACT| together with its action-intent JSON', () => {
    renderAssistant(
      'ACT|{"action_type": "add_medication", "medication": "Paracetamol", "dose": "500 mg"} '
      + 'Requesting paracetamol 500 mg for your approval.'
    );
    expect(
      screen.getByText(/Requesting paracetamol 500 mg for your approval\./)
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('ACT|');
    expect(document.body.textContent).not.toContain('action_type');
  });

  it('strips a marker the model repeated mid-answer', () => {
    renderAssistant('NOACT| Give oxygen. NOACT| Then reassess.');
    expect(document.body.textContent).not.toContain('NOACT|');
    expect(screen.getByText(/Give oxygen\. Then reassess\./)).toBeInTheDocument();
  });

  it('leaves ordinary clinical text alone', () => {
    const answer = 'Sepsis is a life-threatening organ dysfunction.';
    renderAssistant(answer);
    expect(screen.getByText(answer)).toBeInTheDocument();
  });
});

describe('the contract tail is not part of the answer', () => {
  it('cuts a stored <details> block off a reloaded turn', () => {
    renderAssistant(
      'A score of two or more suggests increased risk.'
      + '<details> <cited_sources> ["SEP-001"]</cited_sources> </details>'
    );
    expect(
      screen.getByText(/A score of two or more suggests increased risk\./)
    ).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('cited_sources');
    expect(document.body.textContent).not.toContain('SEP-001');
    expect(document.body.textContent).not.toContain('<details>');
  });

  it('cuts loose section tags the fast lane emits without <details>', () => {
    renderAssistant(
      'NOACT| Give oxygen and reassess.<display_mode> concise '
      + '<patient_facts_summary> None <cited_sources> ["DOC-1"]'
    );
    expect(screen.getByText(/Give oxygen and reassess\./)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('display_mode');
    expect(document.body.textContent).not.toContain('DOC-1');
  });
});
