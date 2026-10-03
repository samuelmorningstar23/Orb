"""HELD-OUT adversarial corpus for the wrong-room detector.

WHY A SECOND CORPUS. `tests/room_fit_corpus.py` was authored alongside the
detector and its suppressors were tuned until that corpus was silent. A
false-positive rate measured on it is therefore a training-set score, and
quoting it would be a marketing claim, not a measurement. Every message below
was written AFTER the detector was frozen, without reading its regexes for
the purpose of dodging them, and none is a paraphrase of a message in that
file.

THE NUMBER THIS FILE EXISTS TO PRODUCE. A wrong-room detector that cries wolf
is ignored inside one shift, and an ignored alarm is worse than no alarm — it
consumes the attention that the real event needed. So the headline metric is
not recall, it is the fire rate on ORDINARY WARD TRAFFIC.

BENIGN is the important half. Each entry is a message a competent charge nurse
would say must NOT raise a wrong-room warning, in the categories that actually
stress the design:

    own_dense    charting about the room's OWN patient, thick with vitals and
                 doses — the likeliest false-positive source, because it is
                 exactly what correct documentation looks like
    comparison   "sicker than bed 4", "same organism as Mr Singh"
    guideline    a protocol quoted with a drug and a dose
    teaching     a teaching point built on another patient's case
    discharged   a message about somebody who has gone home
    correction   a nurse correcting her own earlier message
    logistics    shift, staffing, rounds, transport
    equipment    kit requests, which name beds because kit lives in beds
    handover     several patients enumerated on purpose
    coordination ordinary ward chat

MISROUTES is the false-negative half: genuinely wrong-room clinical detail,
written the way a hurried nurse would actually write it — bed only, MRN only,
initials, a nickname, a misspelling, a name broken across two lines, a fact
phrased as a question, Hinglish, and the Priya Nair / Siddharth Nair surname
collision in both directions.
"""

# ── Roster additions this corpus needs beyond the standard census ────────────
#: A patient who has gone home. Her case room is closed, so the detector must
#: not be able to match her at all — a discharged patient is not a destination.
DISCHARGED = ("P-DIS1", "Shalini Bose", "MRN-GM009", "GM-1", "GM-109")


# ══════════════════════════════════════════════════════════════════════════════
#  BENIGN — ordinary ward traffic. Every one of these SHOULD stay silent.
#  (room_patient_id, category, message)
# ══════════════════════════════════════════════════════════════════════════════
BENIGN: list[tuple[str, str, str]] = [

    # ── Dense, correct charting about the room's OWN patient ───────────────
    # If the detector fires here it is unusable: this is what right looks like.
    ("P-GM3", "own_dense",
     "0600 obs on Priya — temp 38.4, HR 112, BP 104/64, RR 24, spo2 95% on room "
     "air. Gave paracetamol 1 g IV and 500 ml NS over the hour, recheck at 0700."),
    ("P-CC1", "own_dense",
     "Vikram Singh HR 48 on the monitor, BP 92/56. Gave atropine 0.6 mg IV, HR "
     "back up to 72. Repeat ECG done, sinus brady resolved."),
    ("P-PU1", "own_dense",
     "Ravi Shankar's ABG on 4L — pH 7.32, pCO2 58, pO2 61, HCO3 27. Started "
     "BiPAP at 14/6, will repeat the gas in two hours."),
    ("P-GS1", "own_dense",
     "Fatima Begum post-op day 2: temp 37.8, HR 96, wound clean, drain 45 ml "
     "serous. Charted tramadol 50 mg IV TDS and ondansetron 4 mg PRN."),
    ("P-NE1", "own_dense",
     "Savitha Hegde GCS 14, E4 V4 M6. Pupils 3 mm equal and reactive. BP 158/94, "
     "gave labetalol 10 mg IV slowly. Neuro obs hourly as charted."),
    ("P-OR4", "own_dense",
     "Siddharth Nair reporting pain 8/10 on movement. Gave morphine 3 mg IV, "
     "reassessed at 30 minutes, now 3/10. RR 16, spo2 97% on air."),
    ("P-GM4", "own_dense",
     "Mr Reddy's sugars today: fasting 212 at 0600, pre-lunch 188. Increased "
     "actrapid to 8 units TDS as charted. Creatinine 1.4, urea 48."),
    ("P-CC4", "own_dense",
     "Sunita Agarwal weaned to noradrenaline 0.04, MAP holding at 68, urine "
     "output 45 ml/hr. Lactate down from 3.8 to 2.1 on the repeat."),
    ("P-GM3", "own_dense",
     "Priya ka platelet count 42,000 hai, dengue NS1 positive. Fluids 100 ml/hr "
     "chal rahe hain, BP 108/70. Haematology ko bata diya."),
    ("P-PU2", "own_dense",
     "Pooja Malhotra peak flow 210 pre-neb and 320 post. Salbutamol 5 mg with "
     "ipratropium 500 mcg nebulised, hydrocortisone 100 mg IV given."),
    ("P-GS5", "own_dense",
     "Harish Patel's drain has slowed to 20 ml over 8 hours, Hb steady at 10.2. "
     "Restarted enoxaparin 40 mg SC tonight per the surgeon."),
    ("P-NE4", "own_dense",
     "Manoj Kulkarni seizure free for 26 hours now. Levetiracetam 1 g BD "
     "continues, phenytoin level 12.4. BP 128/78, afebrile."),

    # ── Comparisons and referential frames ─────────────────────────────────
    ("P-GM3", "comparison",
     "She's sicker than bed 4 honestly — his BP sat at 118/76 all night and "
     "hers keeps dipping to 88/54."),
    ("P-CC3", "comparison",
     "Same organism as Mr Singh in CCU-01 — klebsiella ESBL. He needed "
     "meropenem 1 g TDS and turned the corner in 48 hours."),
    ("P-OR2", "comparison",
     "This wound looks worse than Meena Kumari's did on day 3, and hers ended "
     "up needing a washout with 2 g cefazolin cover."),
    ("P-NE2", "comparison",
     "Compared with Manoj Kulkarni this one's fits are far better controlled — "
     "Kulkarni needed levetiracetam 1 g BD plus phenytoin on top."),
    ("P-GS3", "comparison",
     "Sicker than Mohammed Ali was pre-op — Ali's lactate was 2.2, this one is "
     "4.6 with a BP of 90/58."),
    ("P-PU3", "comparison",
     "Not as bad as Nandini Das last winter — she went to 15L non-rebreathe and "
     "still desatted to 82%."),
    ("P-GM1", "comparison",
     "Her rash is nothing like Lakshmi Devi's. Devi's was confluent and needed "
     "prednisolone 2 mg/kg for a week."),
    ("P-CC2", "comparison",
     "Same picture as Sunita Agarwal on Tuesday, and she came right with 40 mg "
     "frusemide and a night of CPAP."),

    # ── Guideline / protocol quotations naming a drug and a dose ───────────
    ("P-GS1", "guideline",
     "Surviving Sepsis wants 30 ml/kg crystalloid inside 3 hours and pip-tazo "
     "4.5 g IV q6h. Shall I chart the fluids?"),
    ("P-CC2", "guideline",
     "ESC guideline for rate control in AF is bisoprolol 2.5 mg OD titrated up "
     "to 10 — that is what got Ramesh Gupta down to 78."),
    ("P-GM2", "guideline",
     "Formulary says ceftriaxone 2 g OD for community pneumonia; we gave "
     "Ananya Kapoor 1 g and had to step it up the next day."),
    ("P-NE3", "guideline",
     "As per ICMR the phenytoin load is 20 mg/kg at no more than 50 mg/min, so "
     "that is roughly 1 g over 20 minutes for her."),
    ("P-PU4", "guideline",
     "Trust asthma pathway: back-to-back salbutamol 5 mg nebs, ipratropium "
     "500 mcg, then magnesium 2 g IV if no better at an hour."),

    # ── Teaching points ────────────────────────────────────────────────────
    ("P-GM3", "teaching",
     "Teaching point for the students — Rajesh Iyer's ECG had the classic "
     "saddle ST elevation and we gave 600 mg aspirin on the spot."),
    ("P-CC5", "teaching",
     "Good learning case: Deepak Joshi's troponin went from 0.2 to 1.8 over six "
     "hours, which is exactly why we repeat it at 6h."),
    ("P-OR1", "teaching",
     "For teaching: Ajay Tiwari's fat embolism showed up as confusion and spo2 "
     "88%, not as chest pain. Worth remembering."),
    ("P-NE5", "teaching",
     "Worth knowing for the exam — Usha Rani's LP opening pressure was 32 and "
     "she still had a normal CT head."),

    # ── A patient who has gone home ────────────────────────────────────────
    ("P-GM3", "discharged",
     "Shalini Bose went home on Tuesday on warfarin 3 mg, INR was 2.4 on the "
     "day. Her file is still sitting on the desk if anyone needs it."),
    ("P-GM2", "discharged",
     "Shalini Bose ka discharge summary abhi tak pending hai — warfarin 3 mg "
     "aur INR 2.4 likha hua hai, bas sign hona hai."),

    # ── A nurse correcting her own earlier message ─────────────────────────
    ("P-GM3", "correction",
     "Correction to my 1400 entry — the BP was 118/76, not 108/76. Sorry, "
     "typed it in a hurry."),
    ("P-CC1", "correction",
     "Ignore my last message please, that troponin of 0.42 was bed 3's result, "
     "I picked up the wrong sticker. Vikram's own is 0.11."),
    ("P-PU1", "correction",
     "Please ignore the 0800 obs I posted, wrong chart. Ravi's actual sats were "
     "93% on 2L, not 88%."),
    ("P-NE1", "correction",
     "My earlier entry said lorazepam 2 mg — it was 4 mg IV. Chart has been "
     "corrected and countersigned."),

    # ── Shift and staffing logistics ───────────────────────────────────────
    ("P-GM3", "logistics",
     "I'm off at 8, Priya Reddy is taking this side after me. Handover at 1930 "
     "at the nurses' station."),
    ("P-CC1", "logistics",
     "Two short tonight — Sunita Ghosh is covering the other wing, so bleep me "
     "directly for anything in CCU."),
    ("P-OR4", "logistics",
     "Night roster has changed, Anil Verma has swapped with me for Friday. "
     "Please update the board."),
    ("P-GM3", "logistics",
     "Doctors' round pushed to 10 am, Dr Mehta is stuck in theatre with the "
     "second case."),
    ("P-GS4", "logistics",
     "Transport says the ambulance for the 2 pm transfer is delayed by an hour, "
     "they will ring back."),
    ("P-NE2", "logistics",
     "Please keep the 1400 slot free, MRI have moved two of ours into the same "
     "window and it will run over."),

    # ── Equipment requests (they name beds, because kit lives in beds) ─────
    ("P-CC1", "equipment",
     "Need a working infusion pump in CCU-01, this one keeps alarming at "
     "40 ml/hr for no reason."),
    ("P-PU1", "equipment",
     "Can we get the second BiPAP over from PUL-03? Pillai came off it at 0600 "
     "and his sats held at 94% on 2L."),
    ("P-GM3", "equipment",
     "Ward stock of ondansetron is out, pharmacy says 4 pm. Anyone got a spare "
     "4 mg vial we can borrow?"),
    ("P-NE1", "equipment",
     "The suction unit in NEU-03 is broken again, engineering have been "
     "informed and are coming after lunch."),
    ("P-GS1", "equipment",
     "Please put a size 8 ET tube and a bougie on the airway trolley before the "
     "2 pm list starts."),
    ("P-CC1", "equipment",
     "Blood bank rang about the cross-match for CCU-05 — 2 units are ready for "
     "collection whenever the porter is free."),

    # ── Handover, several patients enumerated on purpose ───────────────────
    ("P-GM3", "handover",
     "Handover 1930: Kapoor BP 118/74 afebrile, Iyer sugars 240 needs a review, "
     "Reddy for CT at 8, Devi INR 4.2 so warfarin held, this bed dengue with "
     "platelets 42."),
    ("P-CC1", "handover",
     "Board round: Menon had 40 mg frusemide at 1400, Joshi trop 1.8 and going "
     "for angio, Agarwal noradrenaline 0.04, Gupta likely home tomorrow."),
    ("P-PU1", "handover",
     "Taking over the pulmonology side — Malhotra's peak flow 320 post neb, "
     "Pillai's sats 94% on 2L, Das ki nebs 4 hourly, Kumar's discharge letter "
     "is done."),
    ("P-OR1", "handover",
     "Sign-out for orthopaedics: Tiwari's Hb 8.9 and needs a repeat, "
     "Venkatesh's dressing changed, Nair's drain out, Bai for physio at 11."),

    # ── Ordinary ward coordination ─────────────────────────────────────────
    ("P-GM3", "coordination",
     "Bed 4's attendant is asking to speak to a doctor, and ours is due her "
     "1400 metformin 500 mg."),
    ("P-CC1", "coordination",
     "Cath lab have confirmed the 11 am slot, consent is signed and he is nil "
     "by mouth from midnight."),
    ("P-GM5", "coordination",
     "Dietician has reviewed and left a plan in the notes, nothing needed from "
     "us overnight."),
    ("P-PU5", "coordination",
     "Sputum has gone to micro, they will ring the result rather than put it on "
     "the system."),
    ("P-GS2", "coordination",
     "Physio saw him this afternoon and he managed the corridor twice, no "
     "desaturation on exertion."),
    ("P-NE3", "coordination",
     "Family have been updated by the registrar and are happy with the plan for "
     "tomorrow."),
]


# ══════════════════════════════════════════════════════════════════════════════
#  MISROUTES — genuinely wrong-room clinical documentation.
#  (room_patient_id, variant, message, expected_patient_id_or_None)
#  `expected` is the patient the detail actually belongs to; None means the
#  reference is legitimately ambiguous and no destination should be named.
# ══════════════════════════════════════════════════════════════════════════════
MISROUTES: list[tuple[str, str, str, str | None]] = [

    # ── No name at all — only a bed ────────────────────────────────────────
    ("P-GM3", "bed_only",
     "Bed 102 has gone into fast AF at 148, gave metoprolol 5 mg IV slowly.",
     "P-GM2"),
    ("P-CC1", "bed_only_qualified",
     "CCU-05 is spiking again, 39.4, blood cultures taken and paracetamol 1 g "
     "IV given.", "P-CC5"),

    # ── No name at all, no bed — the documented blind spot ─────────────────
    ("P-GM3", "no_anchor",
     "BP 78/44, HR 128, gave 500 ml Hartmann's stat and bleeped the reg.", None),

    # ── Only an MRN ────────────────────────────────────────────────────────
    ("P-GM3", "mrn_only",
     "MRN-OR004 wound swab grew staph aureus, started flucloxacillin 1 g QDS.",
     "P-OR4"),

    # ── Initials ───────────────────────────────────────────────────────────
    ("P-GM3", "initials",
     "S.N. in orthopaedics is hypotensive at 88/52, I have given 500 ml stat.",
     "P-OR4"),

    # ── A nickname / short form ────────────────────────────────────────────
    ("P-GM3", "nickname",
     "Sidd's drain has put out 400 ml of frank blood, I have given 1 unit of "
     "packed cells.", "P-OR4"),

    # ── A misspelling ──────────────────────────────────────────────────────
    ("P-GM3", "misspelling",
     "Sidharth Niar's BP is 96/58 and I have started 500 ml Hartmann's.",
     "P-OR4"),
    ("P-CC1", "misspelling_soft",
     "Kavita Menon's potassium is 6.2, gave calcium gluconate 10 ml IV.",
     "P-CC2"),

    # ── A name split across two lines ──────────────────────────────────────
    ("P-GM3", "split_lines",
     "Siddharth\nNair's post-op wound is oozing, BP 96/58, gave 500 ml stat.",
     "P-OR4"),

    # ── Clinical detail phrased as a question ──────────────────────────────
    ("P-GM3", "question_frame",
     "Should I chart Vikram Singh's troponin of 0.9 and the heparin bolus of "
     "4000 units I gave him?", "P-CC1"),
    ("P-CC1", "question_frame_open",
     "Did anyone chart that Lakshmi Devi's BP was 84/50 and I ran 500 ml "
     "saline through her?", "P-GM5"),

    # ── Transliterated Hindi (Hinglish, Latin script) ──────────────────────
    ("P-GM3", "hinglish",
     "Siddharth Nair ka BP 96/58 hai, 500 ml Hartmann's chadha diya.", "P-OR4"),
    ("P-CC1", "hinglish_cue",
     "Menon ki potassium 6.2 aa gayi hai, calcium gluconate 10 ml IV de diya.",
     "P-CC2"),

    # ── A suppressor word worn as camouflage ───────────────────────────────
    # Found adversarially: one bare "previous", doing no historical work at
    # all, silenced a textbook wrong-room entry.
    ("P-GM3", "suppressor_word_camouflage",
     "Adding to the previous — Siddharth Nair's BP is 96/58 and I gave 500 ml "
     "Hartmann's stat.", "P-OR4"),
    ("P-GM3", "temporal_as_as_camouflage",
     "As soon as I saw Siddharth Nair's BP of 96/58 I gave 500 ml Hartmann's "
     "stat.", "P-OR4"),
    ("P-GM3", "bare_apology_camouflage",
     "Apologies for the delay — Siddharth Nair's BP is 96/58 and I gave 500 ml "
     "Hartmann's stat.", "P-OR4"),
    ("P-GM3", "correction_prefix_camouflage",
     "Correction: Siddharth Nair's BP is 96/58 and I gave 500 ml Hartmann's "
     "stat.", "P-OR4"),

    # ── The Priya Nair / Siddharth Nair collision, both directions ─────────
    ("P-OR4", "collision_full_name_a",
     "Priya Nair's platelets are 22,000 and I have started a pool of "
     "platelets.", "P-GM3"),
    ("P-GM3", "collision_full_name_b",
     "Siddharth Nair's haemoglobin has dropped to 7.1, cross-matching 2 units.",
     "P-OR4"),
    ("P-GM3", "collision_bare_surname_own_room",
     "Nair's haemoglobin has dropped to 7.1, cross-matching 2 units.", None),
    ("P-CC1", "collision_bare_surname_third_room",
     "Mr Nair's haemoglobin has dropped to 7.1, cross-matching 2 units.", None),
]
