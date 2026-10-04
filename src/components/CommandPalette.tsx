import React, { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch } from '../api/client';
import { canSeeWardList } from '../lib/roles';
import './CommandPalette.css';

/**
 * Global Cmd-K / Ctrl-K patient search (jump-to-patient).
 *
 * Mount this once inside the clinician shell. It owns its own open/closed state
 * and registers the global Cmd/Ctrl-K keydown listener while mounted, so a later
 * pass only needs to render <CommandPalette /> — no wiring required.
 *
 * On selecting a patient it dispatches, in order:
 *   1. window CustomEvent "orb:goto-tab"     { detail: { tab: "vigil" } }
 *   2. window CustomEvent "orb:open-patient"  { detail: { patientId } }
 * (the Vigil panel + shell tab listeners are owned by other clusters — this
 * component only fires the events).
 *
 * ONLY FOR A ROLE THAT MAY SEARCH PATIENTS. GET /api/search/patients needs
 * patient.read_all (server/routes/search_routes.py), the same permission as the
 * ward list, so a cashier, a pharmacist and the hospital-system desk and lab
 * roles would only ever get a refusal. For them the palette is not offered at
 * all: Cmd/Ctrl-K is not claimed (the browser keeps its own shortcut), nothing
 * renders and nothing is fetched. Doctor, nurse and admin are unchanged.
 */

interface PatientResult {
  id: string;
  name: string;
  mrn: string;
  bed: string | null;
  ward: string | null;
  risk_level: 'critical' | 'elevated' | 'stable' | string | null;
  diagnosis: string | null;
}

function riskColor(level: PatientResult['risk_level']): string {
  if (level === 'critical') return 'var(--status-danger)';
  if (level === 'elevated') return 'var(--status-warn)';
  return 'color-mix(in srgb, var(--status-ok) 75%, transparent)';
}

const LISTBOX_ID = 'orb-command-palette-listbox';
const optionId = (i: number) => `orb-cmdk-option-${i}`;

export default function CommandPalette() {
  // Read from the stored session on every render, as the ward list gate is.
  const canSearch = canSeeWardList();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<PatientResult[]>([]);
  const [activeIndex, setActiveIndex] = useState(0);
  const [loading, setLoading] = useState(false);

  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // Bumped on every keystroke so an in-flight fetch that resolves late can tell
  // it is stale and skip clobbering fresher results.
  const requestSeq = useRef(0);

  const close = useCallback(() => {
    setOpen(false);
    setQuery('');
    setResults([]);
    setActiveIndex(0);
    setLoading(false);
  }, []);

  // ── Global Cmd/Ctrl-K toggle (registered while mounted, for a role that may search) ──
  useEffect(() => {
    if (!canSearch) return;
    const onKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setOpen((prev) => !prev);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [canSearch]);

  // ── Focus the input when the palette opens ──
  useEffect(() => {
    if (open) {
      // Defer to next frame so the element is mounted before we focus it.
      const id = requestAnimationFrame(() => inputRef.current?.focus());
      return () => cancelAnimationFrame(id);
    }
  }, [open]);

  // ── Debounced (200ms) search ──
  useEffect(() => {
    if (!open || !canSearch) return;
    const trimmed = query.trim();
    if (trimmed.length < 2) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- data-fetch/subscribe trigger: the setState is the loader's own loading flag, not state derived from props or state.
      setResults([]);
      setActiveIndex(0);
      setLoading(false);
      return;
    }

    const seq = ++requestSeq.current;
    setLoading(true);
    const handle = window.setTimeout(async () => {
      try {
        const data = await apiFetch<PatientResult[]>(
          `/search/patients?q=${encodeURIComponent(trimmed)}`,
        );
        if (seq !== requestSeq.current) return; // stale response
        setResults(Array.isArray(data) ? data : []);
        setActiveIndex(0);
      } catch {
        if (seq !== requestSeq.current) return;
        setResults([]);
        setActiveIndex(0);
      } finally {
        if (seq === requestSeq.current) setLoading(false);
      }
    }, 200);

    return () => window.clearTimeout(handle);
  }, [query, open, canSearch]);

  // ── Keep the active option scrolled into view ──
  useEffect(() => {
    if (!open) return;
    const el = listRef.current?.querySelector<HTMLElement>(`#${optionId(activeIndex)}`);
    el?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open]);

  const selectPatient = useCallback(
    (patient: PatientResult | undefined) => {
      if (!patient) return;
      // Order matters: switch to the Vigil tab first, THEN ask it to open the
      // patient, so the listener that opens the patient is already mounted.
      window.dispatchEvent(new CustomEvent('orb:goto-tab', { detail: { tab: 'vigil' } }));
      window.dispatchEvent(
        new CustomEvent('orb:open-patient', { detail: { patientId: patient.id } }),
      );
      close();
    },
    [close],
  );

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => (results.length ? (i + 1) % results.length : 0));
      return;
    }
    if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => (results.length ? (i - 1 + results.length) % results.length : 0));
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      selectPatient(results[activeIndex]);
    }
  };

  if (!open || !canSearch) return null;

  const trimmed = query.trim();
  const showEmpty = !loading && trimmed.length >= 2 && results.length === 0;

  return (
    <div
      className="cmdk-overlay"
      onMouseDown={(e) => {
        // Click on the backdrop (not the panel) closes the palette.
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        className="cmdk-panel"
        role="dialog"
        aria-modal="true"
        aria-label="Search patients"
        onKeyDown={onKeyDown}
      >
        <div className="cmdk-input-row">
          <svg
            className="cmdk-search-icon"
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" stroke="currentColor" strokeWidth="2" />
            <line x1="16.5" y1="16.5" x2="21" y2="21" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <input
            ref={inputRef}
            className="cmdk-input"
            type="text"
            placeholder="Search patients by name, MRN, bed, or diagnosis…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            role="combobox"
            aria-expanded={results.length > 0}
            aria-controls={LISTBOX_ID}
            aria-activedescendant={
              results.length > 0 ? optionId(activeIndex) : undefined
            }
            aria-autocomplete="list"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="cmdk-kbd">Esc</kbd>
        </div>

        {(results.length > 0 || showEmpty || loading) && (
          <ul
            ref={listRef}
            className="cmdk-results"
            id={LISTBOX_ID}
            role="listbox"
            aria-label="Patient results"
          >
            {loading && results.length === 0 && (
              <li className="cmdk-status" role="presentation">
                Searching…
              </li>
            )}
            {showEmpty && (
              <li className="cmdk-status" role="presentation">
                No admitted patients match “{trimmed}”.
              </li>
            )}
            {results.map((p, i) => (
              <li
                key={p.id}
                id={optionId(i)}
                role="option"
                aria-selected={i === activeIndex}
                className={`cmdk-option${i === activeIndex ? ' is-active' : ''}`}
                onMouseEnter={() => setActiveIndex(i)}
                onMouseDown={(e) => {
                  // mousedown (not click) so the input doesn't blur first.
                  e.preventDefault();
                  selectPatient(p);
                }}
              >
                <span
                  className="cmdk-risk-dot"
                  style={{ background: riskColor(p.risk_level) }}
                  aria-hidden="true"
                />
                <span className="cmdk-option-main">
                  <span className="cmdk-option-name">{p.name}</span>
                  {p.diagnosis && (
                    <span className="cmdk-option-diagnosis">{p.diagnosis}</span>
                  )}
                </span>
                <span className="cmdk-option-meta">
                  <span className="cmdk-option-mrn">{p.mrn}</span>
                  {p.bed && <span className="cmdk-option-bed">Bed {p.bed}</span>}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
