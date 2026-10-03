import { render } from '@testing-library/react'
import { describe, it, expect } from 'vitest'
import OrbLogo from './OrbLogo'

describe('OrbLogo', () => {
  it('renders the ring and core elements', () => {
    const { container } = render(<OrbLogo />)
    expect(container.querySelector('.orb-logo')).toBeInTheDocument()
    expect(container.querySelector('.orb-logo__ring')).toBeInTheDocument()
    expect(container.querySelector('.orb-logo__core')).toBeInTheDocument()
  })

  it('applies the size prop to width and height', () => {
    const { container } = render(<OrbLogo size={64} />)
    const logo = container.querySelector('.orb-logo') as HTMLElement
    expect(logo.style.width).toBe('64px')
    expect(logo.style.height).toBe('64px')
  })
})
