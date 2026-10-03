/**
 * The Pharmacy store view contract. Later waves add views as single files in
 * this folder and never touch the panel, so what the registry accepts, refuses
 * and orders is pinned here.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { forwardRef, memo } from 'react'

import {
  DEFAULT_VIEW_ORDER,
  discoverViews,
  labelFromId,
  viewIdFromPath,
  visibleViews,
} from './viewRegistry'

function Plain() {
  return null
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('viewIdFromPath', () => {
  it.each([
    ['./pharmacyStore/MastersView.tsx', 'masters'],
    ['./pharmacyStore/VendorReturnsView.tsx', 'vendor-returns'],
    ['./pharmacyStore/WardStockView.tsx', 'ward-stock'],
    ['./pharmacyStore/NdpsView.tsx', 'ndps'],
    ['./pharmacyStore/NDPSRegisterView.tsx', 'ndps-register'],
  ])('%s gives %s', (path, id) => {
    expect(viewIdFromPath(path)).toBe(id)
  })

  it.each([
    './pharmacyStore/View.tsx',
    './pharmacyStore/MastersView.test.tsx',
    './pharmacyStore/viewRegistry.ts',
    './pharmacyStore/Masters.tsx',
  ])('%s is not a view file', (path) => {
    expect(viewIdFromPath(path)).toBeNull()
  })
})

describe('labelFromId', () => {
  it('writes labels in sentence case', () => {
    expect(labelFromId('vendor-returns')).toBe('Vendor returns')
    expect(labelFromId('masters')).toBe('Masters')
  })
})

describe('discoverViews', () => {
  it('derives id and label from the file name when the view declares neither', () => {
    const [entry] = discoverViews({ './pharmacyStore/StockView.tsx': { default: Plain } })
    expect(entry).toMatchObject({
      id: 'stock', label: 'Stock', order: DEFAULT_VIEW_ORDER, permission: null,
      source: './pharmacyStore/StockView.tsx',
    })
    expect(entry.Component).toBe(Plain)
  })

  it('takes the declared id, label, order and permission', () => {
    const [entry] = discoverViews({
      './pharmacyStore/NdpsView.tsx': {
        default: Plain, viewId: 'ndps', viewLabel: ' NDPS register ',
        viewOrder: 80, viewPermission: 'pharmacy_store.ndps',
      },
    })
    expect(entry).toMatchObject({
      id: 'ndps', label: 'NDPS register', order: 80, permission: 'pharmacy_store.ndps',
    })
  })

  it('orders by viewOrder, then label', () => {
    const ids = discoverViews({
      './pharmacyStore/MastersView.tsx': { default: Plain, viewOrder: 90 },
      './pharmacyStore/StockView.tsx': { default: Plain, viewOrder: 40 },
      './pharmacyStore/BView.tsx': { default: Plain },
      './pharmacyStore/AView.tsx': { default: Plain },
      './pharmacyStore/OverviewView.tsx': { default: Plain, viewOrder: 10 },
    }).map(entry => entry.id)
    expect(ids).toEqual(['overview', 'stock', 'masters', 'a', 'b'])
  })

  it('accepts memo and forwardRef components', () => {
    const Memo = memo(Plain)
    const Ref = forwardRef(function RefView() { return null })
    const entries = discoverViews({
      './pharmacyStore/MemoView.tsx': { default: Memo },
      './pharmacyStore/RefView.tsx': { default: Ref },
    })
    expect(entries.map(entry => entry.id)).toEqual(['memo', 'ref'])
  })

  it('leaves out a file with no default component, with a warning', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const entries = discoverViews({
      './pharmacyStore/BrokenView.tsx': { viewLabel: 'Broken' },
      './pharmacyStore/TextView.tsx': { default: 'not a component' },
      './pharmacyStore/StockView.tsx': { default: Plain },
    })
    expect(entries.map(entry => entry.id)).toEqual(['stock'])
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('leaves out a malformed or repeated id, keeping the first file', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const entries = discoverViews({
      './pharmacyStore/AlphaView.tsx': { default: Plain, viewId: 'stock' },
      './pharmacyStore/StockView.tsx': { default: Plain },
      './pharmacyStore/BadView.tsx': { default: Plain, viewId: 'Not An Id' },
    })
    expect(entries.map(entry => [entry.id, entry.source])).toEqual([
      ['stock', './pharmacyStore/AlphaView.tsx'],
    ])
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('ignores a non-numeric order', () => {
    const [entry] = discoverViews({
      './pharmacyStore/StockView.tsx': { default: Plain, viewOrder: '5' },
    })
    expect(entry.order).toBe(DEFAULT_VIEW_ORDER)
  })
})

describe('visibleViews', () => {
  const entries = discoverViews({
    './pharmacyStore/StockView.tsx': { default: Plain },
    './pharmacyStore/NdpsView.tsx': { default: Plain, viewPermission: 'pharmacy_store.ndps' },
  })

  it('keeps ungated views and gated views whose permission is held', () => {
    expect(visibleViews(entries, () => true).map(entry => entry.id)).toEqual(['ndps', 'stock'])
    expect(visibleViews(entries, () => false).map(entry => entry.id)).toEqual(['stock'])
  })

  it('asks about the declared permission only', () => {
    const holds = vi.fn(() => false)
    visibleViews(entries, holds)
    expect(holds).toHaveBeenCalledWith('pharmacy_store.ndps')
    expect(holds).toHaveBeenCalledTimes(1)
  })
})
