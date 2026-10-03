/**
 * A service call opened moments ago has no whole minute on the clock, and the
 * panel printed "Down for None and counting". None is the word for "no
 * downtime"; an open breakdown is never that.
 */
import { describe, expect, it } from 'vitest'

import { downtimeText } from './biomedical'

describe('downtimeText', () => {
  it('says "under a minute" for a breakdown that has only just been reported', () => {
    expect(downtimeText(0, true)).toBe('Down for under a minute and counting')
    expect(downtimeText(null, true)).toBe('Down for under a minute and counting')
  })

  it('names the running time once there is one', () => {
    expect(downtimeText(95, true)).toBe('Down for 1h 35m and counting')
    expect(downtimeText(40145, true)).toBe('Down for 27d 21h and counting')
  })

  it('keeps "None" for a closed call that cost no downtime', () => {
    expect(downtimeText(0, false)).toBe('Downtime None')
    expect(downtimeText(45, false)).toBe('Downtime 45m')
  })
})
