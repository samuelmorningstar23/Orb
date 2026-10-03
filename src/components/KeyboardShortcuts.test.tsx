/**
 * The keyboard layer, and the four ways it can go wrong on a ward.
 *
 *  1. **It fires while a clinician is typing.** Writing "Progress: patient
 *     settled overnight" into a note contains `g` then a letter several times
 *     over. If the layer listens there, the shell navigates away mid-sentence
 *     and the half-written note is behind a tab switch. Pressing `?` in that
 *     same box must produce a question mark, not a dialog.
 *  2. **It fires under a modal.** A tab switch behind an open discharge
 *     confirmation leaves a live clinical dialog floating over a DIFFERENT
 *     patient's board.
 *  3. **It reaches a tab the role cannot open.** RBAC decides the nav rail; a
 *     shortcut must not be a second door past it.
 *  4. **An armed prefix lingers.** A stray `g` that stays armed for minutes
 *     turns the next unrelated keystroke into a navigation.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KeyboardShortcuts from './KeyboardShortcuts'
import { CHORD_TIMEOUT_MS, isModalOpen, isTypingTarget, resolveNavKey } from '../lib/shortcuts'

const ALL_TABS = ['vigil', 'round', 'orders', 'relay', 'sage', 'shift', 'helix']

function mount(tabs: string[] = ALL_TABS) {
  const onGoto = vi.fn()
  render(<KeyboardShortcuts accessibleTabs={tabs} onGoto={onGoto} />)
  return onGoto
}

/** Type a chord at the document, the way a real keypress arrives. */
function chord(...keys: string[]) {
  for (const key of keys) fireEvent.keyDown(window, { key })
}

afterEach(() => {
  /* Unmount through React BEFORE wiping the body. These tests append bare
     nodes to document.body (the fake modal, the input) and the component
     portals into it too — clearing innerHTML first leaves React's auto-cleanup
     trying to remove a node that is already gone. */
  cleanup()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('pure rules', () => {
  it('treats every text surface as a typing target, including contenteditable', () => {
    const input = document.createElement('input')
    const textarea = document.createElement('textarea')
    const select = document.createElement('select')
    const div = document.createElement('div')
    const editable = document.createElement('div')
    // jsdom does not implement isContentEditable from the attribute.
    Object.defineProperty(editable, 'isContentEditable', { value: true })
    const combobox = document.createElement('div')
    combobox.setAttribute('role', 'combobox')

    expect(isTypingTarget(input)).toBe(true)
    expect(isTypingTarget(textarea)).toBe(true)
    expect(isTypingTarget(select)).toBe(true)
    expect(isTypingTarget(editable)).toBe(true)
    expect(isTypingTarget(combobox)).toBe(true)
    expect(isTypingTarget(div)).toBe(false)
    expect(isTypingTarget(null)).toBe(false)
  })

  it('refuses a key mapped to a tab this role cannot reach', () => {
    expect(resolveNavKey('p', ['vigil'])).toBe('vigil')
    // A nurse without the Orders tab presses `g o`: no tab, and no error.
    expect(resolveNavKey('o', ['vigil', 'shift'])).toBeNull()
    expect(resolveNavKey('z', ALL_TABS)).toBeNull()
  })

  it('detects an Orb modal by the aria-modal it already sets', () => {
    expect(isModalOpen()).toBe(false)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)
    expect(isModalOpen()).toBe(true)
  })
})

describe('the global listener', () => {
  it('navigates on the two-key chord', () => {
    const onGoto = mount()
    chord('g', 'p')
    expect(onGoto).toHaveBeenCalledWith('vigil')
  })

  it('does nothing for a single unprefixed letter', () => {
    const onGoto = mount()
    chord('p')
    expect(onGoto).not.toHaveBeenCalled()
  })

  it('never fires while the user is typing, and leaves nothing armed', () => {
    const onGoto = mount()
    const input = document.createElement('input')
    document.body.appendChild(input)

    // "…progress" typed into a note: g, then p.
    fireEvent.keyDown(input, { key: 'g' })
    fireEvent.keyDown(input, { key: 'p' })
    expect(onGoto).not.toHaveBeenCalled()

    // And the `g` must not still be armed after the clinician clicks away.
    chord('p')
    expect(onGoto).not.toHaveBeenCalled()
  })

  it('does not open the sheet when ? is typed into a text field', () => {
    mount()
    const input = document.createElement('input')
    document.body.appendChild(input)
    fireEvent.keyDown(input, { key: '?' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('refuses to navigate underneath an open modal', () => {
    const onGoto = mount()
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)

    chord('g', 'p')
    expect(onGoto).not.toHaveBeenCalled()
  })

  it('yields a key a panel has already consumed, and drops the armed prefix', () => {
    /* The Ward Round binds bare `s` (mark this bed SEEN) on its own panel root
       and calls preventDefault. React's listener sits inside the document, so it
       runs before this window-level one. Without the defaultPrevented guard,
       `g` then `s` on the Ward Round marks a bed seen AND navigates to Sage —
       and one of those is a clinical action. */
    const onGoto = mount()

    // A stand-in for the Ward Round's panel root: a real listener that really
    // consumes the key. `defaultPrevented` is derived from preventDefault(), so
    // it cannot be faked by passing it as an event property.
    const panel = document.createElement('div')
    panel.addEventListener('keydown', (e) => {
      if (e.key === 's') e.preventDefault()
    })
    document.body.appendChild(panel)

    fireEvent.keyDown(panel, { key: 'g' })
    fireEvent.keyDown(panel, { key: 's' })
    expect(onGoto).not.toHaveBeenCalled()

    // And the prefix must not still be armed afterwards.
    fireEvent.keyDown(window, { key: 's' })
    expect(onGoto).not.toHaveBeenCalled()

    // The same chord still works where nothing else claims the key.
    chord('g', 's')
    expect(onGoto).toHaveBeenCalledWith('sage')
  })

  it('yields every modified combination to the browser and the palette', () => {
    const onGoto = mount()
    // Cmd-K must still reach CommandPalette; Cmd-P must still print.
    fireEvent.keyDown(window, { key: 'g', metaKey: true })
    fireEvent.keyDown(window, { key: 'p', metaKey: true })
    fireEvent.keyDown(window, { key: 'g', ctrlKey: true })
    fireEvent.keyDown(window, { key: 'p', ctrlKey: true })
    expect(onGoto).not.toHaveBeenCalled()
  })

  it('lets the armed prefix expire so a stray g cannot eat a later keystroke', () => {
    vi.useFakeTimers()
    const onGoto = mount()

    fireEvent.keyDown(window, { key: 'g' })
    vi.setSystemTime(Date.now() + CHORD_TIMEOUT_MS + 1)
    fireEvent.keyDown(window, { key: 'p' })

    expect(onGoto).not.toHaveBeenCalled()
  })

  it('does not navigate for a role that cannot reach the tab', () => {
    const onGoto = mount(['vigil', 'shift'])
    chord('g', 'o')
    expect(onGoto).not.toHaveBeenCalled()
    chord('g', 'p')
    expect(onGoto).toHaveBeenCalledWith('vigil')
  })
})

describe('the reference sheet', () => {
  beforeEach(() => mount())

  it('opens on ? and closes on Escape', () => {
    expect(screen.queryByRole('dialog')).toBeNull()
    fireEvent.keyDown(window, { key: '?' })
    expect(screen.getByRole('dialog')).toHaveAccessibleName('Keyboard shortcuts')
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not navigate while it is itself open', () => {
    fireEvent.keyDown(window, { key: '?' })
    // The sheet is a modal; reading the reference must not move you.
    fireEvent.keyDown(window, { key: 'g' })
    fireEvent.keyDown(window, { key: 'p' })
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('lists Cmd-K, which exists but was previously undiscoverable', () => {
    fireEvent.keyDown(window, { key: '?' })
    expect(screen.getByText(/Find a patient by name, MRN or bed/)).toBeInTheDocument()
  })
})

describe('the sheet only advertises what this role can actually use', () => {
  it('hides shortcuts for tabs the role cannot open', () => {
    render(<KeyboardShortcuts accessibleTabs={['vigil', 'shift']} onGoto={vi.fn()} />)
    fireEvent.keyDown(window, { key: '?' })

    expect(screen.getByText('Patients')).toBeInTheDocument()
    expect(screen.getByText('My Shift')).toBeInTheDocument()
    expect(screen.queryByText('Orders')).toBeNull()
    expect(screen.queryByText('Pharmacy')).toBeNull()
  })
})
