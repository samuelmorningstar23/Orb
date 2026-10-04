/**
 * Critical limits for one numeric laboratory test (lis-4).
 *
 * Orb ships no critical limit (ruling LIS-R1, held for Samuel to confirm
 * before go-live): a test with no approved limit raises no critical result,
 * and this editor says so in words. A limit is approved by a pathologist with
 * an approval note AND the names of the clinicians it was agreed with, and is
 * never edited: approving the same sex and age band replaces it from now on,
 * and retiring one needs a reason. A value is critical when it is below the
 * lower limit or above the upper one, at the test's reporting precision.
 *
 * The editor itself is the shared BandEditor in ./RangeEditor.tsx, so a range
 * and a limit can never drift apart in how they are approved or retired.
 */
import type { LabCriticalLimit } from '../../../api/lab/master'
import { BandEditor, type BandEditorProps } from './RangeEditor'

export default function CriticalLimitEditor(props: Omit<BandEditorProps, 'kind' | 'current'> & {
  limits: LabCriticalLimit[]
}) {
  const { limits, ...rest } = props
  return <BandEditor kind="limit" current={limits} {...rest} />
}
