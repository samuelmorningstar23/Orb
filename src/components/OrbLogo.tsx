import './OrbLogo.css'

export default function OrbLogo({ size = 48 }: { size?: number }) {
  return (
    <div className="orb-logo" style={{ width: size, height: size }}>
      <div className="orb-logo__ring" />
      <div className="orb-logo__core" />
    </div>
  )
}
