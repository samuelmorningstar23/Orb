import { useState, useEffect } from 'react'
import './ClockWidget.css'
import { formatClinicalDateTime, formatClinicalDayShort } from '../lib/datetime'

export default function ClockWidget() {
  const [time, setTime] = useState(new Date())

  useEffect(() => {
    const timer = setInterval(() => {
      setTime(new Date())
    }, 1000)
    return () => clearInterval(timer)
  }, [])

  const formatTime = (date: Date) => {
    let hours = date.getHours()
    const minutes = date.getMinutes().toString().padStart(2, '0')
    const seconds = date.getSeconds().toString().padStart(2, '0')
    const ampm = hours >= 12 ? 'PM' : 'AM'
    hours = hours % 12
    hours = hours ? hours : 12 // hour '0' should be '12'
    return {
      // Padded like every other clinical time ("06:38 PM"), so the header
      // clock and the chart's timestamps read the same way.
      timeStr: `${String(hours).padStart(2, '0')}:${minutes}`,
      secondsStr: seconds,
      ampm,
    }
  }

  // "Thu 1 Oct", day before month as India writes it ("Thu, Oct 1" was US).
  const formatDate = (date: Date) => formatClinicalDayShort(date)

  const { timeStr, secondsStr, ampm } = formatTime(time)

  return (
    <div className="clock-widget" title={formatClinicalDateTime(time, { seconds: true })}>
      <span className="clock-widget__date">{formatDate(time)}</span>
      <div className="clock-widget__divider" />
      <div className="clock-widget__time-container">
        <span className="clock-widget__time">{timeStr}</span>
        <span className="clock-widget__seconds-dot">:</span>
        <span className="clock-widget__seconds">{secondsStr}</span>
        <span className="clock-widget__ampm">{ampm}</span>
      </div>
    </div>
  )
}
