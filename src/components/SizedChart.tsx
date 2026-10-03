import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'

interface SizedChartProps {
  children: ReactNode
  className?: string
  style?: CSSProperties
}

/**
 * Renders recharts children only while the wrapper div has a measured size.
 * Recharts' ResponsiveContainer emits cx/x NaN and "width(0) and height(0)"
 * console warnings whenever a chart paints inside a zero-size box. That
 * happens on first paint, and again every time the shell hides the panel the
 * chart lives in (panels stay mounted under display:none), so the observer
 * stays attached and the chart unmounts while hidden and remounts on return.
 */
export default function SizedChart({ children, className, style }: SizedChartProps) {
  const ref = useRef<HTMLDivElement>(null)
  const [ready, setReady] = useState(false)

  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const rect = el.getBoundingClientRect()
    setReady(rect.width > 0 && rect.height > 0)
    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setReady(entry.contentRect.width > 0 && entry.contentRect.height > 0)
      }
    })
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  return (
    <div ref={ref} className={className} style={{ width: '100%', height: '100%', ...style }}>
      {ready ? children : null}
    </div>
  )
}
