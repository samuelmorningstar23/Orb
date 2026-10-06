/**
 * Code 128 for specimen labels (lis-8). The encoder must match the reference
 * symbol table and checksums exactly: a label that scans as a different
 * accession is a wrong-patient result.
 */
import { describe, expect, it } from 'vitest'

import {
  CODE128_PATTERNS,
  CODE_C,
  Code128Error,
  QUIET_ZONE_MODULES,
  START_B,
  START_C,
  STOP,
  code128Bars,
  code128ModuleString,
  code128TotalModules,
  encodeCode128,
} from './code128'

const sum = (s: string) => s.split('').reduce((a, c) => a + Number(c), 0)

/** Read widths back into symbol values, as a scanner would. */
function decode(widths: number[]): number[] {
  const lookup = new Map(CODE128_PATTERNS.map((p, value) => [p, value]))
  const out: number[] = []
  let i = 0
  while (i < widths.length) {
    const take = widths.length - i === 7 ? 7 : 6
    const value = lookup.get(widths.slice(i, i + take).join(''))
    if (value === undefined) throw new Error(`no symbol at ${i}`)
    out.push(value)
    i += take
  }
  return out
}

describe('the symbol table', () => {
  it('has 107 distinct symbols of eleven modules, the stop thirteen', () => {
    expect(CODE128_PATTERNS).toHaveLength(107)
    expect(new Set(CODE128_PATTERNS).size).toBe(107)
    CODE128_PATTERNS.slice(0, 106).forEach(p => {
      expect(p).toMatch(/^[1-4]{6}$/)
      expect(sum(p)).toBe(11)
    })
    expect(CODE128_PATTERNS[STOP]).toBe('2331112')
    expect(sum(CODE128_PATTERNS[STOP])).toBe(13)
  })

  it('gives every symbol an even number of bar modules (the code 128 parity rule)', () => {
    CODE128_PATTERNS.forEach(p => {
      const bars = p.split('').filter((_, i) => i % 2 === 0).reduce((a, c) => a + Number(c), 0)
      expect(bars % 2).toBe(0)
    })
  })

  it('matches the reference module patterns', () => {
    const modules = (value: number) => code128ModuleString({
      text: '', startSubset: 'B', codes: [], checksum: 0, modules: 0,
      widths: CODE128_PATTERNS[value].split('').map(Number),
    })
    expect(modules(0)).toBe('11011001100')        // space in subset B
    expect(modules(33)).toBe('10100011000')       // 'A' in subset B
    expect(modules(START_B)).toBe('11010010000')
    expect(modules(START_C)).toBe('11010011100')
    expect(modules(CODE_C)).toBe('10111011110')
    expect(modules(STOP)).toBe('1100011101011')
  })
})

describe('subset C', () => {
  it('packs an even run of digits two to a symbol with the right checksum', () => {
    const enc = encodeCode128('123456')
    expect(enc.startSubset).toBe('C')
    // 105 + 12x1 + 34x2 + 56x3 = 353; 353 mod 103 = 44
    expect(enc.codes).toEqual([START_C, 12, 34, 56, 44, STOP])
    expect(enc.checksum).toBe(44)
  })

  it('encodes a 12-digit accession in ten symbols and 101 modules', () => {
    const enc = encodeCode128('261004000127')
    expect(enc.startSubset).toBe('C')
    expect(enc.codes.slice(0, 7)).toEqual([START_C, 26, 10, 4, 0, 1, 27])
    const expected = (105 + 26 * 1 + 10 * 2 + 4 * 3 + 0 * 4 + 1 * 5 + 27 * 6) % 103
    expect(enc.checksum).toBe(expected)
    expect(enc.codes).toHaveLength(9)
    expect(enc.modules).toBe(11 * 8 + 13)
    expect(decode(enc.widths)).toEqual(enc.codes)
  })
})

describe('subset B', () => {
  it('matches the reference checksum for PJJ123C', () => {
    const enc = encodeCode128('PJJ123C')
    expect(enc.startSubset).toBe('B')
    expect(enc.codes).toEqual([START_B, 48, 42, 42, 17, 18, 19, 35, 55, STOP])
    expect(enc.checksum).toBe(55)
    expect(decode(enc.widths)).toEqual(enc.codes)
  })

  it('writes a site prefix in B and switches to C for the digits', () => {
    const enc = encodeCode128('AB261004000127')
    expect(enc.codes.slice(0, 4)).toEqual([START_B, 33, 34, CODE_C])
    expect(enc.codes.slice(4, 10)).toEqual([26, 10, 4, 0, 1, 27])
    let total = enc.codes[0]
    enc.codes.slice(1, -2).forEach((c, i) => { total += c * (i + 1) })
    expect(enc.checksum).toBe(total % 103)
    expect(decode(enc.widths)).toEqual(enc.codes)
  })

  it('keeps an odd leading digit in B before switching', () => {
    const enc = encodeCode128('12345')
    expect(enc.codes.slice(0, 5)).toEqual([START_B, 17, CODE_C, 23, 45])
  })

  it('refuses empty text and characters outside printable ASCII', () => {
    expect(() => encodeCode128('')).toThrow(Code128Error)
    expect(() => encodeCode128('café')).toThrow(Code128Error)
    expect(() => encodeCode128('a\tb')).toThrow(Code128Error)
  })
})

describe('bars', () => {
  it('draws one rectangle per bar after the quiet zone, ending on the stop bar', () => {
    const enc = encodeCode128('123456')
    const bars = code128Bars(enc)
    expect(bars[0]).toEqual({ x: QUIET_ZONE_MODULES, width: 2 })
    expect(bars).toHaveLength(enc.widths.length / 2 + 0.5)
    const last = bars[bars.length - 1]
    expect(last.x + last.width).toBe(QUIET_ZONE_MODULES + enc.modules)
    expect(code128TotalModules(enc)).toBe(enc.modules + 2 * QUIET_ZONE_MODULES)
  })
})
