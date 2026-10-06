import { describe, expect, it } from 'vitest'
import { actorLabel } from './actorLabel'

describe('actorLabel', () => {
  it('reads "Name (id)" when both are known', () => {
    expect(actorLabel('Dr Meera Iyer', 'D-101')).toBe('Dr Meera Iyer (D-101)')
  })
  it('keeps the id alone when the user is gone', () => {
    expect(actorLabel(null, 'D-404')).toBe('D-404')
  })
  it('is empty when there is no actor at all', () => {
    expect(actorLabel(undefined, null)).toBe('')
  })
})
