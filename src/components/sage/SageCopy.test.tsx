/**
 * Sage's words to the person using it: who it greets, how it labels a past
 * conversation, and the one tagline it uses everywhere.
 *
 * Every user used to be "Dr. <surname>", so a nurse read "Good evening,
 * Dr. Reddy"; a chat about a patient was labelled "General Medical" because
 * the label had nothing to read; and three different taglines named Sage.
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SageEmptyState, SAGE_TAGLINE } from './SageEmptyState';
import { greetingName } from './greetingName';
import { SageHistorySidebar } from './SageHistorySidebar';
import en from '../../i18n/locales/en.json';

describe('greetingName', () => {
  it('greets a nurse by first name, never as a doctor', () => {
    expect(greetingName('Priya Reddy')).toBe('Priya');
  });

  it('keeps Dr. for a name stored with it', () => {
    expect(greetingName('Dr. Meera Sharma')).toBe('Dr. Sharma');
    expect(greetingName('Dr Vikram Rathore')).toBe('Dr. Rathore');
  });

  it('says no name rather than a guessed one', () => {
    expect(greetingName(undefined)).toBe('');
    expect(greetingName('   ')).toBe('');
    expect(greetingName('Dr.')).toBe('');
  });
});

describe('SageEmptyState', () => {
  it('greets a nurse without "Dr." and shows no emoji', () => {
    render(<SageEmptyState userName="Priya Reddy" onQuickAction={vi.fn()} />);
    const title = screen.getByRole('heading', { level: 1 });
    expect(title.textContent).toMatch(/, Priya$/);
    expect(document.body.textContent).not.toContain('Dr.');
    expect(document.body.textContent).not.toMatch(/\p{Extended_Pictographic}/u);
    expect(document.body.textContent).not.toContain('census');
  });

  it('greets with no name at all when none is known', () => {
    render(<SageEmptyState onQuickAction={vi.fn()} />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toMatch(/^Good (morning|afternoon|evening)$/);
  });
});

describe('one tagline', () => {
  it('is the same on the landing screen, the sidebar and the nav', () => {
    render(<SageEmptyState userName="Priya Reddy" onQuickAction={vi.fn()} />);
    expect(document.body.textContent).toContain(SAGE_TAGLINE);
    expect(en.subtitle.sage).toBe(SAGE_TAGLINE);
  });
});

describe('SageHistorySidebar', () => {
  const chat = (content: string, extra: object = {}) => ({ role: 'user', content, index: 0, ...extra });

  it('labels a thread with its patient when the server knows them', () => {
    render(<SageHistorySidebar recentChats={[chat('how is she?', { patientName: 'Lakshmi Devi' })]}
      onSelectChat={vi.fn()} onNewChat={vi.fn()} />);
    expect(screen.getByText('Patient: Lakshmi Devi')).toBeInTheDocument();
    expect(document.body.textContent).toContain(SAGE_TAGLINE);
  });

  it('labels nothing rather than "General Medical" when the patient is unknown', () => {
    render(<SageHistorySidebar recentChats={[chat('what is the qSOFA?')]}
      onSelectChat={vi.fn()} onNewChat={vi.fn()} />);
    expect(document.body.textContent).not.toContain('General Medical');
    expect(document.body.textContent).not.toContain('Patient:');
  });
});
