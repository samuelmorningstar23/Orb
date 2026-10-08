/**
 * BedPicker: where an admitted patient will lie, chosen from bed setup
 * (adt-5). Shown on the admission form only while Beds and admissions
 * (ORB_ADT) is on; with it off the form keeps its typed ward and bed.
 *
 * WHAT IT ASKS FOR, AND WHY. Every rule here is the server's, and the server
 * enforces it whatever this screen shows; the picker asks only for what the
 * server will ask for, before the round trip:
 *   - an occupied bed cannot be chosen; 'Add an extra bed beside it' admits
 *     the patient to a temporary bed with its own label (ruling ADT-S1 (a));
 *   - a bed housekeeping has not recorded clean needs a typed reason, and one
 *     whose next clean is terminal needs a doctor, nurse or administrator who
 *     authorises it, with the reason (ruling ADT-S2 (c)): the person sending
 *     the form, when their role allows, or someone who types their own
 *     signing PIN here. Choosing a name is never enough;
 *   - a bed out of service needs a typed reason (as well as the authorisation
 *     when its next clean is terminal);
 *   - a ward owned by another department makes the patient a boarder: the
 *     words say who will and will not be able to open the chart, and must be
 *     confirmed (ruling ADT-S6 is not applied, so scope does not change);
 *   - 'No bed yet' admits to the ward awaiting a bed;
 *   - a ward not in bed setup can still be typed: the admission is kept
 *     unlinked until a bed manager links it.
 * The bed's words (Available, Needs cleaning, Occupied) are the server's,
 * printed as sent. Nothing about who lies in an occupied bed reaches here.
 */
import { useId } from 'react'

import {
  TYPED_WARD,
  authorisersFor,
  authorisesThemself,
  boarderWords,
  chosenBed,
  chosenWard,
  isBoarder,
  needsOutOfServiceReason,
  type AdmissionMaster,
  type BedChoice,
  type MasterBedChoice,
} from '../../api/adt/moves'
import './BedPicker.css'

export interface BedPickerProps {
  master: AdmissionMaster
  /** The treating department chosen on the form. */
  department: string
  value: BedChoice
  onChange: (next: BedChoice) => void
  /** The server's last refusal about the place, in its own words. */
  refusal?: { code: string; message: string } | null
}

const NO_BED = '__no_bed__'

function bedTitle(bed: MasterBedChoice): string {
  return `Bed ${bed.bed_label ?? bed.unit_code}`
}

export default function BedPicker({ master, department, value, onChange, refusal }: BedPickerProps) {
  const uid = useId()
  const ward = chosenWard(master, value)
  const bed = chosenBed(master, value)
  const boarder = isBoarder(ward, department)

  const set = (patch: Partial<BedChoice>) => onChange({ ...value, ...patch })

  const cleared = {
    outOfServiceReason: '', notCleanReason: '', terminalAuthorisedBy: '',
    terminalAuthoriserPin: '', terminalReason: '',
  }

  const chooseWard = (wardId: string) => onChange({
    ...value, wardId, bedId: '', awaiting: false, extraBed: false, boarderAcknowledged: false,
    ...cleared,
  })

  const chooseBed = (bedId: string) => onChange({
    ...value, bedId: bedId === NO_BED ? '' : bedId, awaiting: bedId === NO_BED, extraBed: false,
    ...cleared,
  })

  const addExtraBed = (beside: MasterBedChoice) => onChange({
    ...value, bedId: beside.bed_id, awaiting: false, extraBed: true, ...cleared,
  })

  const offered = authorisersFor(master, ward, department)
  const self = master.self_authoriser ?? null
  const needsPin = !!value.terminalAuthorisedBy && !authorisesThemself(master, value)

  const selected = value.awaiting ? NO_BED : (value.extraBed ? '' : value.bedId)

  return (
    <fieldset className="bed-picker">
      <legend className="bed-picker__legend">Ward and bed <span aria-hidden="true">*</span></legend>

      <label className="bed-picker__field" htmlFor={`${uid}-ward`}>
        <span className="bed-picker__label">Ward</span>
        <select
          id={`${uid}-ward`}
          className="admit-input"
          value={value.wardId}
          onChange={(e) => chooseWard(e.target.value)}
        >
          <option value="">Select...</option>
          {master.wards.map((w) => (
            <option key={w.id} value={w.id}>
              {w.code}{w.name && w.name !== w.code ? ` (${w.name})` : ''}
              {w.department ? `, ${w.department}` : ''}
            </option>
          ))}
          <option value={TYPED_WARD}>A ward not in bed setup</option>
        </select>
      </label>

      {value.wardId === TYPED_WARD && (
        <div className="bed-picker__typed">
          <label className="bed-picker__field" htmlFor={`${uid}-typed-ward`}>
            <span className="bed-picker__label">Ward as written</span>
            <input
              id={`${uid}-typed-ward`}
              className="admit-input"
              value={value.typedWard}
              onChange={(e) => set({ typedWard: e.target.value })}
            />
          </label>
          <label className="bed-picker__field" htmlFor={`${uid}-typed-bed`}>
            <span className="bed-picker__label">Bed as written</span>
            <input
              id={`${uid}-typed-bed`}
              className="admit-input"
              value={value.typedBed}
              onChange={(e) => set({ typedBed: e.target.value, awaiting: !e.target.value.trim() })}
            />
          </label>
          <p className="bed-picker__note" role="note">
            This place is not in bed setup, so the admission will not be linked to a bed. A bed
            manager links it once the ward and bed are set up.
          </p>
        </div>
      )}

      {ward && (
        <>
          {boarder && (
            <div className="bed-picker__note bed-picker__note--warn" role="note">
              <p className="bed-picker__words">
                This ward belongs to another department, so the patient lies here as a
                boarder. {boarderWords(ward, department)}
              </p>
              <label className="bed-picker__check">
                <input
                  type="checkbox"
                  checked={value.boarderAcknowledged}
                  onChange={(e) => set({ boarderAcknowledged: e.target.checked })}
                />
                <span>I have read this. Admit as a boarder.</span>
              </label>
            </div>
          )}

          <div className="bed-picker__beds" role="radiogroup" aria-label={`Beds on ${ward.code}`}>
            <label className={`bed-picker__bed${selected === NO_BED ? ' bed-picker__bed--on' : ''}`}>
              <input
                type="radio"
                name={`${uid}-bed`}
                value={NO_BED}
                checked={selected === NO_BED}
                onChange={() => chooseBed(NO_BED)}
              />
              <span className="bed-picker__bed-title">No bed yet</span>
              <span className="bed-picker__bed-status">Admit to the ward, awaiting a bed</span>
            </label>
            {ward.beds.map((b) => (
              <div
                key={b.bed_id}
                className={`bed-picker__bed bed-picker__bed--${b.tone}${selected === b.bed_id ? ' bed-picker__bed--on' : ''}${!b.selectable ? ' bed-picker__bed--closed' : ''}`}
              >
                <label className="bed-picker__bed-pick">
                  <input
                    type="radio"
                    name={`${uid}-bed`}
                    value={b.bed_id}
                    checked={selected === b.bed_id}
                    disabled={!b.selectable}
                    onChange={() => chooseBed(b.bed_id)}
                  />
                  <span className="bed-picker__bed-title">{bedTitle(b)}</span>
                  <span className="bed-picker__bed-status">{b.status_words}</span>
                </label>
                {(b.isolation || b.temporary || b.room) && (
                  <span className="bed-picker__chips">
                    {b.room && <span className="bed-picker__chip">Room {b.room}</span>}
                    {b.isolation && <span className="bed-picker__chip">Isolation</span>}
                    {b.temporary && <span className="bed-picker__chip">Extra bed</span>}
                  </span>
                )}
                {b.occupied && (
                  <button
                    type="button"
                    className="admit-btn bed-picker__extra"
                    aria-pressed={value.extraBed && value.bedId === b.bed_id}
                    onClick={() => addExtraBed(b)}
                  >
                    Add an extra bed beside {b.bed_label}
                  </button>
                )}
              </div>
            ))}
            {ward.beds.length === 0 && (
              <p className="bed-picker__note">No beds are set up on {ward.code} yet.</p>
            )}
          </div>

          {value.extraBed && bed && (
            <p className="bed-picker__note" role="note">
              An extra bed with its own label ({bed.bed_label}A, or the next free letter) is
              added beside bed {bed.bed_label}, and the patient is admitted to it. It is counted
              as an extra bed until a bed manager retires it.
            </p>
          )}

          {!value.extraBed && needsOutOfServiceReason(bed) && bed && (
            <label className="bed-picker__field" htmlFor={`${uid}-oos`}>
              <span className="bed-picker__label">
                {bedTitle(bed)} is out of service. Reason the patient is placed there
              </span>
              <input
                id={`${uid}-oos`}
                className="admit-input"
                value={value.outOfServiceReason}
                onChange={(e) => set({ outOfServiceReason: e.target.value })}
              />
            </label>
          )}

          {!value.extraBed && bed?.needs === 'not_clean_reason' && (
            <label className="bed-picker__field" htmlFor={`${uid}-clean`}>
              <span className="bed-picker__label">
                {bedTitle(bed)} has not been recorded clean. Reason the patient is placed there now
              </span>
              <input
                id={`${uid}-clean`}
                className="admit-input"
                value={value.notCleanReason}
                onChange={(e) => set({ notCleanReason: e.target.value })}
              />
            </label>
          )}

          {!value.extraBed && bed?.needs === 'terminal_clean_authorisation' && (
            <div className="bed-picker__note bed-picker__note--warn" role="group"
              aria-label="Authorise a bed awaiting a terminal clean">
              <p className="bed-picker__words">
                {bedTitle(bed)} is waiting for a terminal clean (an isolation room, or an
                isolation patient left it). It can be used before that clean only if a doctor,
                nurse or administrator authorises it, with the reason: you, if your role allows,
                or someone who types their own signing PIN here. Both are recorded.
              </p>
              <label className="bed-picker__field" htmlFor={`${uid}-auth`}>
                <span className="bed-picker__label">Authorised by</span>
                <select
                  id={`${uid}-auth`}
                  className="admit-input"
                  value={value.terminalAuthorisedBy}
                  onChange={(e) => set({ terminalAuthorisedBy: e.target.value, terminalAuthoriserPin: '' })}
                >
                  <option value="">Select...</option>
                  {offered.map((a) => (
                    <option key={a.id} value={a.id}>
                      {self && a.id === self.id ? `You (${a.name})` : a.name}
                      {a.department ? `, ${a.department}` : ''}
                    </option>
                  ))}
                </select>
              </label>
              {offered.length === 0 && (
                <p className="bed-picker__note" role="note">
                  Nobody here can authorise this bed: it needs a doctor, nurse or administrator
                  of this ward or the treating team with a signing PIN. Choose another bed.
                </p>
              )}
              {needsPin && (
                <label className="bed-picker__field" htmlFor={`${uid}-auth-pin`}>
                  <span className="bed-picker__label">Their signing PIN, typed by them</span>
                  <input
                    id={`${uid}-auth-pin`}
                    className="admit-input"
                    type="password"
                    inputMode="numeric"
                    autoComplete="off"
                    value={value.terminalAuthoriserPin}
                    onChange={(e) => set({ terminalAuthoriserPin: e.target.value })}
                  />
                </label>
              )}
              <label className="bed-picker__field" htmlFor={`${uid}-auth-reason`}>
                <span className="bed-picker__label">Reason</span>
                <input
                  id={`${uid}-auth-reason`}
                  className="admit-input"
                  value={value.terminalReason}
                  onChange={(e) => set({ terminalReason: e.target.value })}
                />
              </label>
            </div>
          )}
        </>
      )}

      {refusal && (
        <p className="bed-picker__refusal" role="alert">{refusal.message}</p>
      )}
    </fieldset>
  )
}
