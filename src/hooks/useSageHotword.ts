import { useState, useCallback } from 'react'

interface UseSageHotwordReturn {
  isEnabled: boolean
  toggleEnabled: () => void
}

export function useSageHotword(): UseSageHotwordReturn {
  // Default OFF. The hotword drives continuous microphone capture; leaving it on
  // by default meant every clinician shell streamed ward-room audio (patient
  // conversations = PHI) to the browser's speech recogniser without consent,
  // which contradicts Orb's on-prem / PHI-never-leaves-the-building posture. It
  // is now strictly opt-in and remembered per browser.
  const [isEnabled, setIsEnabled] = useState(() => {
    const stored = localStorage.getItem('orb_sage_hotword')
    return stored === 'true'
  })

  const toggleEnabled = useCallback(() => {
    setIsEnabled(prev => {
      const next = !prev
      localStorage.setItem('orb_sage_hotword', String(next))
      return next
    })
  }, [])

  return { isEnabled, toggleEnabled }
}

