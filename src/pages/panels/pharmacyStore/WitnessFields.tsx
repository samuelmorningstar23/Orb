/**
 * The witness picker and signing PIN box shared by the NDPS register and the
 * vendor return screens. The server checks everything again: who may
 * witness, that it is somebody else, the PIN and its lockout.
 */
export interface WitnessOption {
  id: string
  name: string
}

export default function WitnessFields({ idPrefix, witnesses, witnessId, setWitnessId, pin,
  setPin }: {
  idPrefix: string
  witnesses: WitnessOption[]
  witnessId: string
  setWitnessId: (id: string) => void
  pin: string
  setPin: (pin: string) => void
}) {
  return (
    <div className="psw-fields">
      <div className="psw-field">
        <label htmlFor={`${idPrefix}-witness`}>Witness</label>
        <select id={`${idPrefix}-witness`} value={witnessId}
          onChange={e => setWitnessId(e.target.value)}>
          <option value="">Choose a pharmacist</option>
          {witnesses.map(w => <option key={w.id} value={w.id}>{w.name}</option>)}
        </select>
      </div>
      <div className="psw-field">
        <label htmlFor={`${idPrefix}-pin`}>Witness signing PIN</label>
        <input id={`${idPrefix}-pin`} type="password" inputMode="numeric" autoComplete="off"
          value={pin} onChange={e => setPin(e.target.value)} />
      </div>
      {witnesses.length === 0 && (
        <p className="psw-notice psw-notice--warn">
          Nobody else here can witness controlled stock yet. A second pharmacist with the
          NDPS permission and a signing PIN is needed.
        </p>
      )}
    </div>
  )
}
