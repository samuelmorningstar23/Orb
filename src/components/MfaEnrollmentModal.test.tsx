/**
 * MFA enrolment modal — QR rendering contract.
 *
 * The server's enroll/begin payload MAY carry `qr_svg` (an SVG QR of the
 * otpauth URI as a data: URI). Two things must hold on the client:
 *
 *   1. When the field is present, the QR renders as an <img> ABOVE the manual
 *      setup key, with a "can't scan?" pointer to the key — scanning is the
 *      fast path, the key is the fallback.
 *   2. When the field is absent (appliance without the qrcode package), the
 *      modal degrades to exactly the manual key-entry flow: no broken image,
 *      no dangling "scan the QR" instruction, setup key + copy flow intact.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({
  apiFetch: vi.fn(),
}))

import { apiFetch } from '../api/client'
import MfaEnrollmentModal from './MfaEnrollmentModal'

const apiFetchMock = vi.mocked(apiFetch)

const QR_DATA_URI =
  'data:image/svg+xml;base64,' +
  btoa('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 45 45"><path d="M4,4H5V5H4z"/></svg>')

const BEGIN_BASE = {
  secret: 'JBSWY3DPEHPK3PXP',
  otpauth_uri: 'otpauth://totp/Orb:D001?secret=JBSWY3DPEHPK3PXP&issuer=Orb',
  issuer: 'Orb',
}

describe('MfaEnrollmentModal QR rendering', () => {
  beforeEach(() => {
    apiFetchMock.mockReset()
  })

  it('renders the server QR above the manual key when qr_svg is present', async () => {
    apiFetchMock.mockResolvedValue({ ...BEGIN_BASE, qr_svg: QR_DATA_URI })

    render(<MfaEnrollmentModal onClose={() => {}} />)

    const img = await screen.findByAltText(/QR code for authenticator enrolment/i)
    expect(img).toBeInTheDocument()
    expect(img).toHaveAttribute('src', QR_DATA_URI)

    // The manual key stays available as the fallback, with its copy flow…
    const key = screen.getByText('JBSW Y3DP EHPK 3PXP')
    expect(key).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Copy' }).length).toBeGreaterThan(0)
    // …and is pointed to for anyone who cannot scan.
    expect(screen.getByText(/use the setup key below/i)).toBeInTheDocument()

    // The QR sits ABOVE the manual key in the document.
    expect(img.compareDocumentPosition(key) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('falls back to manual key entry when qr_svg is absent', async () => {
    apiFetchMock.mockResolvedValue({ ...BEGIN_BASE })

    render(<MfaEnrollmentModal onClose={() => {}} />)

    // Wait for the enroll view (the manual key) to appear.
    await waitFor(() => {
      expect(screen.getByText('JBSW Y3DP EHPK 3PXP')).toBeInTheDocument()
    })

    // No image at all — not a broken <img>, and no scan instruction.
    expect(screen.queryByRole('img')).toBeNull()
    expect(screen.queryByText(/scan the qr code/i)).toBeNull()
    // Manual-entry instructions and copy flow are intact.
    expect(screen.getByText(/manual key entry/i)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Copy' }).length).toBeGreaterThan(0)
  })
})
