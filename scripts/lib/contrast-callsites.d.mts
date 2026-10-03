// Types for the parts of the call-site scanner its self-test uses
// (src/test/contrastCallsites.test.ts). The module itself is plain ESM run by
// node from scripts/check-contrast.mjs.
export type RGBA = number[]
export interface Variant { value: string; cond: string | null; branch: 'T' | 'F' | null }
export interface CallSite {
  kind: string
  where: string
  file: string
  line: number
  colors: Variant[]
  backgrounds: Variant[]
  fontSize?: string
  fontWeight?: string
  opacity?: number | null
  onlyTheme: string | null
}
export function scanTsx(file: string, root: string): CallSite[]
export function resolveValue(raw: string, tokens: Record<string, string>, base: Record<string, string>): RGBA | null
export function measureSite(
  site: CallSite,
  tokens: Record<string, string>,
  base: Record<string, string>,
  surfaces: number[][],
  themeName: string,
): { ratio: number; need: number; fg: string; bg: string } | null
