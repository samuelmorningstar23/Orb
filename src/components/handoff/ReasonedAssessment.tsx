/**
 * TIER 3 ON SCREEN — Sage's four-section assessment, where a clinician can
 * actually read it.
 *
 * ═══ WHY THIS FILE EXISTS ═══
 * The server side of this shipped complete and correct and then sat there
 * unread. `GET /api/ipass/{id}/reasoned-assessment` returned 200 with
 * `available: true`, the heading, four considerations and four watch items —
 * and the handover page contained the string "Sage" zero times. Relocating the
 * 46-second assessment off the alert and onto the handover and the ward round
 * was the whole point of the tier split; until this component it had been
 * relocated to nowhere.
 *
 * ═══ THE ONE RULE THIS COMPONENT OBEYS ═══
 * IT REWORDS NOTHING. The heading, the AI-drafted label and the disclaimer are
 * medico-legal labelling. The server holds them as constants
 * (`ipass_service.AI_SUPPLEMENT_HEADING` / `AI_DRAFTED_LABEL` /
 * `AI_SUPPLEMENT_DISCLAIMER`) specifically so that the printed sheet, the saved
 * scribe note and every screen carry the same words, and a client that
 * shortens one to fit a column has quietly broken the property. So they are
 * printed exactly as they arrive and there is no client-side fallback text for
 * any of them. The same holds for every machine enum: the server ships a
 * `*_display` twin already phrased for a ward, and the twin is what is printed.
 *
 * The connective sentences below ("What Sage said was happening", "Reproduced
 * here exactly as it was written…") are COPIED VERBATIM from the server's own
 * `ipass_service._render_assessment_lines`, so the screen and the printout say
 * the same thing rather than two versions of it. `test_assessment_relocation`
 * pins each of them by name against this file.
 *
 * ═══ ABSENCE IS NOT A STATE WORTH RENDERING ═══
 * No assessment, or an assessment with nothing to say, renders NOTHING — no
 * heading, no "unavailable" chip, no spinner. That is the server's stated
 * contract on this endpoint and it is a clinical judgement, not a style one:
 * absence of a machine opinion is not a fact worth a line on a ward round, and
 * a spinner would be promising an assessment that may be 40 seconds away or may
 * never arrive at all.
 *
 * ═══ WHY ITS OWN STYLESHEET ═══
 * The shared clinician primitives live in pages/ClinicianShell.css, but this
 * renders on `/handoff/:patientId` too, which is a standalone route outside the
 * shell and never loads that file. A colocated stylesheet is the pattern the
 * round already uses (components/round/Round.css) and is the only one that
 * cannot leave one of the two surfaces unstyled.
 */
import type { IpassReasonedAssessment } from '../../api/ipass';
import { ASSESSMENT_COPY } from './reasonedAssessmentCopy';
import './ReasonedAssessment.css';

interface Props {
  assessment: IpassReasonedAssessment | null;
}


export function ReasonedAssessment({ assessment }: Props) {
  // Rule 3, client side: never a heading over nothing. The server already
  // refuses to carry a draft whose prose is empty; this is the same refusal one
  // layer out, so a shape change upstream cannot put a medico-legal heading on
  // screen with no assessment under it.
  if (!assessment || !assessment.heading || !(assessment.whats_happening || '').trim()) {
    return null;
  }

  const considerations = (assessment.considerations || []).filter(
    c => (c.step || '').trim() !== '',
  );
  const watchItems = (assessment.watch_items || []).filter(w => (w || '').trim() !== '');
  const evidence = (assessment.evidence || []).filter(e => (e.title || '').trim() !== '');

  const author = assessment.model
    ? `Author: ${assessment.authored_by}, model ${assessment.model}.`
    : `Author: ${assessment.authored_by}.`;
  const written = assessment.drafted_at_display
    ? `Written ${assessment.age_display} (${assessment.drafted_at_display})`
    : `Written ${assessment.age_display}`;

  return (
    <section className="orb-assess" aria-label={assessment.heading}>
      {/* The labelling, verbatim, before a word of the assessment itself. */}
      <h3 className="orb-assess__heading">{assessment.heading}</h3>
      <p className="orb-assess__label">{assessment.label}</p>

      <div className="orb-assess__provenance">
        <p className="orb-assess__line">{author}</p>
        <p className="orb-assess__line">
          {written}, {assessment.reuse_basis_display}. {ASSESSMENT_COPY.reproduced}
        </p>
        {/* THE SNAPSHOT SENTENCE. Everything else on a handover sheet is
            current; this section is not, and a supplement that reads as current
            is worse than no supplement. */}
        <p className="orb-assess__line">
          {ASSESSMENT_COPY.snapshotLead}
          {assessment.news2_context}
          {ASSESSMENT_COPY.snapshotTail}
        </p>
        <p className="orb-assess__line">
          {assessment.endorsed_by
            ? `Reviewed and endorsed by ${assessment.endorsed_by}.`
            : `Review status: ${assessment.state_display}.`}
        </p>
      </div>

      <p className="orb-assess__disclaimer">{assessment.disclaimer}</p>

      <h4 className="orb-assess__sub">{ASSESSMENT_COPY.whatsHappening}</h4>
      <p className="orb-assess__prose">{assessment.whats_happening}</p>

      {considerations.length > 0 && (
        <>
          <h4 className="orb-assess__sub">{ASSESSMENT_COPY.steps}</h4>
          <ol className="orb-assess__steps">
            {considerations.map((c, i) => (
              <li key={i} className="orb-assess__step">
                <span className="orb-assess__stepText">
                  {c.step}
                  {(c.why || '').trim() ? `, ${c.why}` : ''}
                </span>
                {c.orb_can_act && (
                  <span className="orb-assess__card">{ASSESSMENT_COPY.orbCanAct}</span>
                )}
              </li>
            ))}
          </ol>
        </>
      )}

      {watchItems.length > 0 && (
        <>
          <h4 className="orb-assess__sub">{ASSESSMENT_COPY.watch}</h4>
          <ul className="orb-assess__watch">
            {watchItems.map((w, i) => (
              <li key={i}>{w}</li>
            ))}
          </ul>
        </>
      )}

      <p className="orb-assess__footer">
        {`Evidence: ${assessment.citation_display}`}
        {ASSESSMENT_COPY.confidenceLead}
        {`${assessment.confidence_display}.`}
      </p>
      {evidence.length > 0 && (
        <ul className="orb-assess__evidence">
          {evidence.map((e, i) => (
            <li key={i}>
              {e.title}
              {(e.source || '').trim() ? ` (${e.source})` : ''}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

export default ReasonedAssessment;
