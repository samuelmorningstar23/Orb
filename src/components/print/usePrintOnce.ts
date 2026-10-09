import { useEffect, useRef } from 'react'

/**
 * The print lifecycle the print views share (SpecimenLabelPrintView,
 * OrdersPrintView): once the portal has committed, open the browser's print
 * dialog after two animation frames, and call `onAfterPrint` on `afterprint`,
 * which fires whether the user printed or cancelled, so the caller can take
 * the portal down. With `autoPrint` false (tests) nothing is opened.
 */
export function usePrintOnce(onAfterPrint: () => void, autoPrint = true): void {
  const done = useRef(onAfterPrint)
  useEffect(() => { done.current = onAfterPrint }, [onAfterPrint])

  useEffect(() => {
    if (!autoPrint) return undefined
    const handleAfterPrint = () => done.current()
    window.addEventListener('afterprint', handleAfterPrint)
    let cancelled = false
    const rafId = requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (cancelled) return
        window.print()
      })
    })
    return () => {
      cancelled = true
      cancelAnimationFrame(rafId)
      window.removeEventListener('afterprint', handleAfterPrint)
    }
  }, [autoPrint])
}
