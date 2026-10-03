/** Orb navigation bus — window CustomEvents that let any surface (Sage, the
 *  command palette, deep links) drive the shell without prop-drilling.
 *
 *  orb:goto-tab     detail: { tab: string }
 *  orb:open-patient detail: { patientId: string, name?: string }
 *  orb:sage-ask     detail: { question: string }
 *
 *  ClinicianShell listens for goto-tab; VigilPanel listens for open-patient
 *  (switching to the vigil tab first when needed is the dispatcher's job —
 *  use openPatient(), which does both).
 */

export const GOTO_TAB_EVENT = 'orb:goto-tab'
export const OPEN_PATIENT_EVENT = 'orb:open-patient'
export const SAGE_ASK_EVENT = 'orb:sage-ask'
/** Open the keyboard reference. On the bus rather than prop-drilled because the
 *  sheet's state belongs to KeyboardShortcuts (which owns the key listener) and
 *  the button that opens it lives in the header, several levels away. A keyboard
 *  layer nobody can find is a keyboard layer nobody uses. */
export const SHOW_SHORTCUTS_EVENT = 'orb:show-shortcuts'

export function gotoTab(tab: string): void {
  window.dispatchEvent(new CustomEvent(GOTO_TAB_EVENT, { detail: { tab } }))
}

export function openPatient(patientId: string, name?: string): void {
  gotoTab('vigil')
  window.dispatchEvent(new CustomEvent(OPEN_PATIENT_EVENT, { detail: { patientId, name } }))
}

/* ── Carrying a question into Sage ──────────────────────────────────────────
 *
 * The doctor home's inline composer hands its question to the Sage panel. That
 * cannot be a bare CustomEvent, because of WHEN it fires: `gotoTab('sage')`
 * switches the shell's active tab, and SagePanel is a LAZY chunk that has not
 * been fetched — let alone mounted — at the moment the event dispatches. A
 * listener that does not exist yet cannot hear anything, so on the first ask of
 * a session the question would vanish and the doctor would land on an empty
 * composer with no idea why.
 *
 * So the question is LATCHED as well as announced. A panel already mounted
 * hears `orb:sage-ask` immediately; a panel mounting afterwards drains the
 * latch with takePendingSageQuestion(). Exactly one of the two paths delivers
 * it — the latch is cleared by whichever runs first.
 *
 * Deliberately module state and not sessionStorage: a clinical question is PHI-
 * adjacent free text and a ward tablet is shared, so it must not outlive the
 * page, let alone the session.
 */
let pendingSageQuestion: string | null = null

/** Send a question to Sage and switch to its tab. Mirrors openPatient(). */
export function askSage(question: string): void {
  const text = question.trim()
  if (!text) return
  pendingSageQuestion = text
  gotoTab('sage')
  window.dispatchEvent(new CustomEvent(SAGE_ASK_EVENT, { detail: { question: text } }))
}

/** Read and CLEAR the latched question. Call once, on mount. */
export function takePendingSageQuestion(): string | null {
  const q = pendingSageQuestion
  pendingSageQuestion = null
  return q
}

export function onSageAsk(handler: (question: string) => void): () => void {
  const listener = (e: Event) => {
    const q = (e as CustomEvent).detail?.question
    if (typeof q === 'string' && q) {
      pendingSageQuestion = null
      handler(q)
    }
  }
  window.addEventListener(SAGE_ASK_EVENT, listener)
  return () => window.removeEventListener(SAGE_ASK_EVENT, listener)
}

export function showShortcuts(): void {
  window.dispatchEvent(new CustomEvent(SHOW_SHORTCUTS_EVENT))
}

export function onShowShortcuts(handler: () => void): () => void {
  const listener = () => handler()
  window.addEventListener(SHOW_SHORTCUTS_EVENT, listener)
  return () => window.removeEventListener(SHOW_SHORTCUTS_EVENT, listener)
}

export function onGotoTab(handler: (tab: string) => void): () => void {
  const listener = (e: Event) => {
    const tab = (e as CustomEvent).detail?.tab
    if (typeof tab === 'string' && tab) handler(tab)
  }
  window.addEventListener(GOTO_TAB_EVENT, listener)
  return () => window.removeEventListener(GOTO_TAB_EVENT, listener)
}

export function onOpenPatient(handler: (patientId: string, name?: string) => void): () => void {
  const listener = (e: Event) => {
    const d = (e as CustomEvent).detail
    if (d && typeof d.patientId === 'string' && d.patientId) handler(d.patientId, d.name)
  }
  window.addEventListener(OPEN_PATIENT_EVENT, listener)
  return () => window.removeEventListener(OPEN_PATIENT_EVENT, listener)
}
