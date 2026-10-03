/**
 * The server appends its investigational banner (lens_service
 * LENS_DISCLAIMER_BANNER) to every Lens stream, and the saved analysis keeps it.
 * The editable findings box must not: a disclaimer the clinician can select and
 * delete is not a disclaimer. The panel shows the label as fixed text instead,
 * and the assign route adds it to what is filed.
 */
const BANNER_MARK = 'INVESTIGATIONAL. NOT FOR DIAGNOSTIC USE.'

/** The streamed text without the appended banner (and the rule line before it). */
export function withoutAppendedDisclaimer(text: string): string {
  const at = text.lastIndexOf(BANNER_MARK)
  if (at < 0) return text
  return text
    .slice(0, at)
    // the warning sign (U+26A0, U+FE0F) and the "---" rule that lead the banner
    .replace(/(?:\s|\u{26A0}|\u{FE0F})*$/u, '')
    .replace(/\s*-{3,}\s*$/, '')
    .trimEnd()
}
