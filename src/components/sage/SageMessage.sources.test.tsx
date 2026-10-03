/**
 * Where an answer's evidence came from, in words.
 *
 * The model names no source on most turns, so the server attributes the
 * documents the answer demonstrably used ('system_attributed'). The panel
 * badged only the model's own citations, so Orb's match was invisible, and an
 * answer resting on no document at all looked like a grounded one.
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SageMessage, SageStageStatus } from './SageMessage';
import { noSourcesNote, sourcesHeading } from './sageSources';

const SOURCES = [
  { document_id: 'EXT-1', title: 'Sepsis screening', trust_tier: 3, cited: true, attribution: 'model_cited', fusion_score: 0.3 },
  { document_id: 'POL-2', title: 'Fluids policy', trust_tier: 1, cited: false, attribution: 'system_attributed', fusion_score: 0.2 },
  { document_id: 'EXT-3', title: 'Lactate guide', trust_tier: 3, cited: false, attribution: 'context_only', fusion_score: 0.1 },
];

function renderAnswer(metadata: object) {
  return render(
    <SageMessage
      message={{ role: 'assistant', content: 'Give fluids and repeat the lactate.', metadata }}
      isStreaming={false}
      isSpeaking={false}
    />
  );
}

function openDetails() {
  fireEvent.click(screen.getByRole('button', { name: /show details/i }));
}

describe('each source says who tied it to the answer', () => {
  it('labels cited, matched and unused documents', () => {
    renderAnswer({ sources: SOURCES, citation_basis: 'model_cited', cited_count: 1 });
    openDetails();
    expect(screen.getByText('Cited by Sage')).toBeInTheDocument();
    expect(screen.getByText('Matched by Orb')).toBeInTheDocument();
    expect(screen.getByText('Retrieved, not used')).toBeInTheDocument();
  });

  it('heads a system-attributed answer as Orb\'s match, not Sage\'s citation', () => {
    renderAnswer({ sources: SOURCES.slice(1), citation_basis: 'system_attributed', attributed_count: 1 });
    expect(screen.getByRole('button', { name: /1 matched by Orb/ })).toBeInTheDocument();
    openDetails();
    expect(screen.getByText(/Sources Orb matched to this answer/)).toBeInTheDocument();
    expect(screen.getByText(/Sage did not name a source/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('Sources Sage cited');
  });

  it('falls back to the cited flag when the server sent no attribution', () => {
    renderAnswer({ sources: [{ document_id: 'X', title: 'Old row', cited: true }], citation_basis: 'model_cited' });
    openDetails();
    expect(screen.getByText('Cited by Sage')).toBeInTheDocument();
  });

  it('labels a sample document as not this hospital\'s policy', () => {
    renderAnswer({ sources: [{ ...SOURCES[1], is_synthetic: true }], citation_basis: 'system_attributed' });
    openDetails();
    expect(document.body.textContent).toContain("sample document, not this hospital's policy");
  });

  it('maps every basis to a heading', () => {
    expect(sourcesHeading('model_cited')).toBe('Sources Sage cited');
    expect(sourcesHeading('system_attributed')).toBe('Sources Orb matched to this answer');
    expect(sourcesHeading('retrieved')).toBe('Documents Sage read');
  });
});

describe("an answer resting on no document says so", () => {
  it('shows one line for basis none', () => {
    renderAnswer({ sources: [], citation_basis: 'none' });
    expect(screen.getByText(/The answer rests on the model alone/)).toBeInTheDocument();
  });

  it('names the chart when one was used', () => {
    expect(noSourcesNote('none', true)).toMatch(/the chart and the model alone/);
  });

  // The four landing quick actions are ward-wide: the server answers them from
  // the department census with citation_basis 'none', scope BROAD.
  it('names the ward records, not the model alone, on a census answer', () => {
    renderAnswer({
      sources: [], citation_basis: 'none', query_scope: 'BROAD',
      confidence_basis: 'deterministic_scope', patient_context_used: false,
    });
    expect(screen.getByText(/Built from the hospital records Orb read/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain('model alone');
  });

  it('names the records on a count answer and on any record-built scope', () => {
    expect(noSourcesNote('none', false, 'AGGREGATE_STATS')).toMatch(/hospital records Orb read/);
    expect(noSourcesNote('none', false, 'DISAMBIGUATE')).toMatch(/hospital records Orb read/);
    expect(noSourcesNote('none', false, undefined, 'deterministic_scope')).toMatch(/hospital records Orb read/);
  });

  it('keeps the model-alone line for a general question with nothing retrieved', () => {
    expect(noSourcesNote('none', false, 'GENERAL_MEDICAL', 'no_evidence_retrieved'))
      .toMatch(/rests on the model alone/);
  });

  it('says nothing for a grounded answer', () => {
    renderAnswer({ sources: SOURCES, citation_basis: 'model_cited' });
    expect(document.body.textContent).not.toContain('model alone');
    expect(noSourcesNote('retrieved', false)).toBeNull();
  });
});

describe('SageStageStatus', () => {
  afterEach(() => { vi.useRealTimers(); });

  it('counts the seconds since the question was sent', () => {
    vi.useFakeTimers();
    const start = Date.now();
    render(<SageStageStatus stage="generating" startedAt={start} />);
    expect(screen.getByRole('status').textContent).toContain('Writing the answer');
    expect(screen.getByRole('status').textContent).toContain('0 s');
    act(() => { vi.advanceTimersByTime(3000); });
    expect(screen.getByRole('status').textContent).toContain('3 s');
    expect(screen.getByRole('status').textContent).not.toContain('minute or two');
  });

  it('names the search stage', () => {
    render(<SageStageStatus stage="retrieving" startedAt={Date.now()} />);
    expect(screen.getByRole('status').textContent).toContain('Searching clinical guidelines');
  });
});
