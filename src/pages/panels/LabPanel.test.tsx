/**
 * LabPanel: the shell of the laboratory module, whose screens arrive one file
 * at a time (lis-1). What it must never get wrong:
 *
 * 1. SWITCHED OFF IS NOT EMPTY. A 404 module_disabled is the shell's
 *    switched-off notice, never a laboratory with nothing in it.
 * 2. DRAINING IS SAID IN WORDS (ruling LIS-R21). Switched off with open work,
 *    the panel still opens, says new work has stopped, and hides the views
 *    that only start new work.
 * 3. NO SCREENS YET IS SAID HONESTLY, and says where laboratory results are
 *    meanwhile, rather than rendering a blank frame.
 * 4. A VIEW GATED ON A PERMISSION IS HIDDEN unless the permission is held.
 * 5. THE PANEL NAMES NO VIEW. Views come in as entries (from the glob in the
 *    real build), in their declared order, and can switch to one another.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import i18n from '../../i18n'
import LabPanel from './LabPanel'
import {
  discoverViews,
  labelFromId,
  viewIdFromPath,
  visibleViews,
  type LabViewEntry,
  type LabViewProps,
} from './lab/viewRegistry'
import { LabApiError } from '../../api/lab'

vi.mock('../../api/lab', async () => {
  const actual = await vi.importActual<typeof import('../../api/lab')>('../../api/lab')
  return { ...actual, getLabMeta: vi.fn() }
})

const api = await import('../../api/lab')
const getMeta = vi.mocked(api.getLabMeta)

beforeAll(async () => {
  await i18n.changeLanguage('en')
})

function WorklistsView({ state, openView }: LabViewProps) {
  return (
    <div>
      <p>Worklists body, module {state}</p>
      <button type="button" onClick={() => openView('reports')}>Go to reports</button>
      <button type="button" onClick={() => openView('not-installed')}>Go nowhere</button>
    </div>
  )
}

function ReportsView({ onModuleOff }: LabViewProps) {
  return (
    <div>
      <p>Reports body</p>
      <button type="button" onClick={onModuleOff}>Report the module switched off</button>
    </div>
  )
}

function CollectView() {
  return <p>Collect body</p>
}

function SetupView() {
  return <p>Setup body</p>
}

/** Built the way the real glob result is, so the registry is exercised too. */
const VIEWS: LabViewEntry[] = discoverViews({
  './lab/ReportsView.tsx': { default: ReportsView, viewOrder: 70 },
  './lab/WorklistsView.tsx': { default: WorklistsView, viewOrder: 30 },
  './lab/CollectView.tsx': { default: CollectView, viewOrder: 10, viewWhenDraining: 'hide' },
  './lab/SetupView.tsx': { default: SetupView, viewOrder: 90, viewPermission: 'lab.master' },
})

const holdsAll = () => true
const holdsNone = () => false

beforeEach(() => {
  getMeta.mockReset()
})

describe('LabPanel', () => {
  it('says it is loading while the module probe is in flight', () => {
    getMeta.mockReturnValue(new Promise(() => {}))
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading the laboratory')
  })

  it('shows the switched-off notice, never an empty laboratory, on module_disabled', async () => {
    getMeta.mockRejectedValue(new LabApiError('The laboratory module is switched off on this site.',
      404, 'module_disabled', { error: 'module_disabled', module: 'lab' }))
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    expect(await screen.findByText('Laboratory is switched off on this site.')).toBeInTheDocument()
    expect(screen.queryByText('No laboratory screens yet')).not.toBeInTheDocument()
    expect(screen.queryByText(/Worklists body/)).not.toBeInTheDocument()
  })

  it('offers a retry when the server cannot be reached', async () => {
    getMeta.mockRejectedValueOnce(new LabApiError(
      'Could not reach the laboratory. Check the connection and try again.', 0, 'network_error'))
    getMeta.mockResolvedValueOnce({ module: 'lab', state: 'on' })
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the laboratory')
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByText('Collect body')).toBeInTheDocument()
    expect(getMeta).toHaveBeenCalledTimes(2)
  })

  it('says honestly that no screens are installed yet', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel views={[]} holds={holdsAll} />)
    expect(await screen.findByText('No laboratory screens yet')).toBeInTheDocument()
    expect(screen.getByText(/stay on the Orders screen/)).toBeInTheDocument()
  })

  it('the real build mounts with no props and finds its views in ./lab/', async () => {
    // Amended in lis-4 (wave 3): views now ship in ./lab/, so the real build is
    // no longer empty. With no signed-in role nothing is held, so every gated
    // view stays hidden and the panel says so; with none installed it would
    // say that instead. Either way it renders, and never a blank frame.
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel />)
    expect(await screen.findByText(/Nothing here for your role|No laboratory screens yet/))
      .toBeInTheDocument()
  })

  it('shows the views in their declared order and hides a view the role cannot use', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel views={VIEWS} holds={(p) => p !== 'lab.master'} />)
    const tabs = await screen.findAllByRole('button', { name: /Collect|Worklists|Reports|Setup/ })
    expect(tabs.map((t) => t.textContent)).toEqual(['Collect', 'Worklists', 'Reports'])
    expect(screen.getByText('Collect body')).toBeInTheDocument()
  })

  it('says so when no installed view is open to this role', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel views={[VIEWS.find((v) => v.id === 'setup')!]} holds={holdsNone} />)
    expect(await screen.findByText('Nothing here for your role')).toBeInTheDocument()
  })

  it('switches views from a tab and from inside a view, and ignores an unknown id', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Worklists' }))
    expect(screen.getByText('Worklists body, module on')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Go nowhere' }))
    expect(screen.getByText('Worklists body, module on')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Go to reports' }))
    expect(screen.getByText('Reports body')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reports' })).toHaveAttribute('aria-current', 'page')
  })

  it('while draining, says new work has stopped and hides the views that start it', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'draining' })
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    expect(await screen.findByText('The laboratory module is switched off on this site'))
      .toBeInTheDocument()
    expect(screen.getByText(/New laboratory work has stopped/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Collect' })).not.toBeInTheDocument()
    expect(screen.getByText('Worklists body, module draining')).toBeInTheDocument()
  })

  it('a view that meets module_disabled later replaces every view with the notice', async () => {
    getMeta.mockResolvedValue({ module: 'lab', state: 'on' })
    render(<LabPanel views={VIEWS} holds={holdsAll} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reports' }))
    fireEvent.click(screen.getByRole('button', { name: 'Report the module switched off' }))
    await waitFor(() => expect(screen.getByText('Laboratory is switched off on this site.'))
      .toBeInTheDocument())
    expect(screen.queryByText('Reports body')).not.toBeInTheDocument()
  })
})

describe('the laboratory view registry', () => {
  it('derives ids and labels from the file name', () => {
    expect(viewIdFromPath('./lab/CriticalResultsView.tsx')).toBe('critical-results')
    expect(viewIdFromPath('./lab/TATView.tsx')).toBe('tat')
    expect(viewIdFromPath('./lab/viewRegistry.ts')).toBeNull()
    expect(labelFromId('critical-results')).toBe('Critical results')
  })

  it('leaves out a file with no component, a bad id or a repeated id', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const found = discoverViews({
      './lab/NoComponentView.tsx': { viewOrder: 1 },
      './lab/BadIdView.tsx': { default: CollectView, viewId: 'Bad Id' },
      './lab/OneView.tsx': { default: CollectView, viewId: 'same' },
      './lab/TwoView.tsx': { default: SetupView, viewId: 'same' },
    })
    expect(found.map((v) => v.source)).toEqual(['./lab/OneView.tsx'])
    expect(warn).toHaveBeenCalledTimes(3)
    warn.mockRestore()
  })

  it('a view shows while draining unless it says hide', () => {
    expect(visibleViews(VIEWS, holdsAll, 'draining').map((v) => v.id))
      .toEqual(['worklists', 'reports', 'setup'])
    expect(visibleViews(VIEWS, holdsAll, 'on').map((v) => v.id))
      .toEqual(['collect', 'worklists', 'reports', 'setup'])
  })
})
