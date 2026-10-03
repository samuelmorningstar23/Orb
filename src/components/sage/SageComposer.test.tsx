/**
 * The thinking-mode control in the Sage composer.
 *
 * What is pinned here is legibility, not styling: a clinician must be able to
 * tell which brain is about to answer WITHOUT clicking anything. That means a
 * visible label (not an icon alone), a correct switch role for screen readers,
 * and a standing notice while the fast lane is armed.
 */
import { fireEvent, render, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SageComposer } from './SageComposer';
import { getThinkingMode, setThinkingMode } from '../../api/thinkingMode';

type ComposerProps = ComponentProps<typeof SageComposer>;

function renderComposer(overrides: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    query: '',
    setQuery: vi.fn(),
    onSend: vi.fn(),
    onStop: vi.fn(),
    isStreaming: false,
    voiceMode: false,
    toggleVoiceMode: vi.fn(),
    voiceRecording: false,
    startVoiceRecording: vi.fn(),
    stopVoiceRecording: vi.fn(),
    isSpeaking: false,
    ...overrides,
  };
  return render(<SageComposer {...props} />);
}

beforeEach(() => {
  localStorage.clear();
  setThinkingMode(true);
});

describe('thinking-mode toggle', () => {
  it('starts ON and says so in words', () => {
    renderComposer();
    const toggle = screen.getByRole('switch', { name: /thinking mode/i });
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(toggle).toHaveTextContent('Thinking');
    expect(screen.queryByText(/Fast mode\./)).not.toBeInTheDocument();
  });

  it('switching it off changes the LABEL, not just an icon', () => {
    renderComposer();
    const toggle = screen.getByRole('switch', { name: /thinking mode/i });
    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute('aria-checked', 'false');
    expect(toggle).toHaveTextContent('Fast');
    expect(getThinkingMode()).toBe(false);
  });

  it('arming fast mode raises a standing notice that also states the floor', () => {
    renderComposer();
    fireEvent.click(screen.getByRole('switch', { name: /thinking mode/i }));

    const notice = screen.getByRole('status');
    expect(notice).toHaveTextContent(/Fast mode\./);
    expect(notice).toHaveTextContent(/small model/i);
    // The half-truth this guards against: "fast mode is on" without saying
    // which questions still escape it would make the floor look like a bug.
    expect(notice).toHaveTextContent(/Patient, medication and dose questions/i);
  });

  it('reflects a value chosen before this component mounted', () => {
    setThinkingMode(false);
    renderComposer();
    expect(screen.getByRole('switch', { name: /thinking mode/i }))
      .toHaveAttribute('aria-checked', 'false');
  });

  it('toggling back restores the careful state and clears the notice', () => {
    renderComposer();
    const toggle = screen.getByRole('switch', { name: /thinking mode/i });
    fireEvent.click(toggle);
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute('aria-checked', 'true');
    expect(screen.queryByText(/Fast mode\./)).not.toBeInTheDocument();
  });
});
