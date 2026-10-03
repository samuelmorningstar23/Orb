"""Ward-chatter corpus for the wrong-room detector.

WHY THIS FILE EXISTS AND WHAT IT IS NOT. Orb is pre-pilot: the live database
holds 8 human text messages and 74,733 bot alert rows. There is no observed ward
chatter to measure a false-positive rate against, so this corpus is AUTHORED,
not sampled — and every number derived from it must be read as "on chatter of
this shape", not "on your ward". It is written against the real six-department
census so the hard cases (Priya Nair / Siddharth Nair; nurse "Priya Reddy" vs
patient "Priya Nair" and patient "Suresh Reddy"; Dr Amit Patel vs patient Harish
Patel) are the genuine collisions this hospital actually contains.

NEGATIVES are the important half. A detector is trivially made sensitive; the
work is in not firing on these.
"""

# ── The live census (server/scripts/seed_clinical_data.py) ───────────────────
CENSUS = [
    # (patient_id, name, mrn, ward, bed)
    ("P-GM1", "Ananya Kapoor",    "MRN-GM001", "GM-1", "GM-101"),
    ("P-GM2", "Rajesh Iyer",      "MRN-GM002", "GM-1", "GM-102"),
    ("P-GM3", "Priya Nair",       "MRN-GM003", "GM-1", "GM-103"),
    ("P-GM4", "Suresh Reddy",     "MRN-GM004", "GM-1", "GM-104"),
    ("P-GM5", "Lakshmi Devi",     "MRN-GM005", "GM-1", "GM-105"),
    ("P-CC1", "Vikram Singh",     "MRN-CC001", "CCU",  "CCU-01"),
    ("P-CC2", "Kavitha Menon",    "MRN-CC002", "CCU",  "CCU-02"),
    ("P-CC3", "Deepak Joshi",     "MRN-CC003", "CCU",  "CCU-03"),
    ("P-CC4", "Sunita Agarwal",   "MRN-CC004", "CCU",  "CCU-04"),
    ("P-CC5", "Ramesh Gupta",     "MRN-CC005", "CCU",  "CCU-05"),
    ("P-NE1", "Savitha Hegde",    "MRN-NE001", "NEU",  "NEU-01"),
    ("P-NE2", "Prakash Shetty",   "MRN-NE002", "NEU",  "NEU-02"),
    ("P-NE3", "Usha Rani",        "MRN-NE003", "NEU",  "NEU-03"),
    ("P-NE4", "Manoj Kulkarni",   "MRN-NE004", "NEU",  "NEU-04"),
    ("P-NE5", "Indira Patil",     "MRN-NE005", "NEU",  "NEU-05"),
    ("P-OR1", "Meena Kumari",     "MRN-OR001", "ORT",  "ORT-01"),
    ("P-OR2", "Ajay Tiwari",      "MRN-OR002", "ORT",  "ORT-02"),
    ("P-OR3", "Radha Venkatesh",  "MRN-OR003", "ORT",  "ORT-03"),
    ("P-OR4", "Siddharth Nair",   "MRN-OR004", "ORT",  "ORT-04"),
    ("P-OR5", "Kamala Bai",       "MRN-OR005", "ORT",  "ORT-05"),
    ("P-PU1", "Ravi Shankar",     "MRN-PU001", "PUL",  "PUL-01"),
    ("P-PU2", "Pooja Malhotra",   "MRN-PU002", "PUL",  "PUL-02"),
    ("P-PU3", "Ganesh Pillai",    "MRN-PU003", "PUL",  "PUL-03"),
    ("P-PU4", "Nandini Das",      "MRN-PU004", "PUL",  "PUL-04"),
    ("P-PU5", "Vinod Kumar",      "MRN-PU005", "PUL",  "PUL-05"),
    ("P-GS1", "Fatima Begum",     "MRN-GS001", "GS-1", "GS-101"),
    ("P-GS2", "Mohammed Ali",     "MRN-GS002", "GS-1", "GS-102"),
    ("P-GS3", "Arun Krishnan",    "MRN-GS003", "GS-1", "GS-103"),
    ("P-GS4", "Geeta Sharma",     "MRN-GS004", "GS-1", "GS-104"),
    ("P-GS5", "Harish Patel",     "MRN-GS005", "GS-1", "GS-105"),
]

#: Real staff names from the seeded roster. Several collide with patient names —
#: that is the point, not an accident of the fixture.
STAFF_NAMES = [
    "Orb Sage", "Orb Vigil",
    "Dr. Meera Sharma", "Dr. Arjun Mehta", "Dr. Vikram Rathore",
    "Dr. Nandini Rao", "Dr. Amit Patel", "Dr. Sanjay Rao",
    "Dr. Anita Desai", "Dr. Kiran Bhat",
    "Priya Reddy", "Sunita Ghosh", "Anil Verma",
]


# ══════════════════════════════════════════════════════════════════════════════
#  NEGATIVES — ordinary ward chatter. Every one of these MUST stay silent.
#  (room_patient_id, message)
# ══════════════════════════════════════════════════════════════════════════════
NEGATIVES: list[tuple[str, str]] = [
    # ── Plain charting about the room's own patient ──
    ("P-GM3", "BP 138/86, HR 92, spo2 97% on room air. Comfortable this morning."),
    ("P-GM3", "Gave paracetamol 650 mg PO at 0800 for the headache."),
    ("P-GM3", "Priya's morning bloods are back — Hb 11.4, creatinine 0.9, all fine."),
    ("P-GM3", "Nair is due for her 1400 dose of metformin 500 mg."),
    ("P-GM3", "Mrs Nair had a settled night, no further episodes of dizziness."),
    ("P-CC1", "Troponin 0.42 at 6 hours, up from 0.31. ECG unchanged."),
    ("P-CC1", "Started him on IV heparin per weight, 4000 units bolus then infusion."),
    ("P-CC1", "Vikram is much more comfortable after the GTN. BP now 118/72."),
    ("P-PU1", "SpO2 dropped to 88% on 2L, put him on 40% venturi, sats now 94%."),
    ("P-GS1", "Wound looks clean, no discharge. Drain out 60 ml serous overnight."),
    ("P-OR4", "Physio saw him, mobilising with a frame. Pain 4/10 on movement."),
    ("P-NE1", "GCS 15 throughout the night, pupils equal and reactive."),

    # ── Legitimate MENTION of another patient — the whole point of the feature ──
    ("P-GM3", "Bed 104 is sicker than this one, I'll do him first."),
    ("P-GM3", "Same organism as Mr Singh in CCU — pseudomonas."),
    ("P-CC1", "Similar picture to Kavitha Menon last week, she responded to diuresis."),
    ("P-GM3", "Compared to Suresh Reddy this is a much milder presentation."),
    ("P-PU1", "Just like Ganesh Pillai, he desaturates only on exertion."),
    ("P-GS1", "Unlike Mohammed Ali she has no peritonism at all."),
    ("P-OR4", "We used the same protocol as for Meena Kumari, worked well."),
    ("P-NE1", "This is the same seizure pattern we saw with Prakash Shetty."),

    # ── Questions about another patient (mention, not documentation) ──
    ("P-GM3", "Did we ever get Mr Reddy's lactate back from this morning?"),
    ("P-CC1", "Should we consider the same dose of furosemide we gave Ramesh Gupta?"),
    ("P-GM3", "Has anyone seen Lakshmi Devi's discharge summary?"),

    # ── Handover / list framing — several patients on purpose ──
    ("P-GM3", "Handover: Kapoor stable BP 120/80, Iyer needs bloods at 6, "
              "Reddy for CT at 10, Devi awaiting physio."),
    ("P-CC1", "Night list — Menon 40 mg frusemide given, Joshi troponin 0.9, "
              "Agarwal for angio in the morning, Gupta stable."),
    ("P-PU1", "Shift summary: Malhotra sats 91% on 2L, Pillai afebrile, "
              "Das nebs 4 hourly, Kumar for discharge."),

    # ── Guideline / protocol quoting a drug and a dose ──
    ("P-GM3", "Per the sepsis guideline we should give 30 ml/kg crystalloid within the hour."),
    ("P-CC1", "The trust protocol says aspirin 300 mg then 75 mg daily. Shall we follow it?"),
    ("P-PU1", "As per BNF the max dose of salbutamol nebs is 5 mg four times daily."),
    ("P-GS1", "NICE recommends co-amoxiclav 1.2 g IV TDS for this indication."),
    ("P-NE1", "Guideline for status: lorazepam 4 mg IV, repeat once after 10 minutes."),

    # ── Staff names that collide with patient names (real roster collisions) ──
    ("P-GM3", "Dr Sharma reviewed and charted amlodipine 5 mg OD."),
    ("P-GS5", "Dr Amit Patel is covering tonight, bleep him if the BP drops below 90."),
    ("P-PU4", "Dr Nandini Rao gave the pre-med, 2 mg midazolam IV."),
    ("P-GM3", "Priya Reddy took the 0800 obs — BP 132/78, HR 88."),
    ("P-GM4", "Staff nurse Reddy has charted the 40 mg dose already."),
    ("P-CC1", "Sister Sharma says the bed in CCU-04 will be free after 2pm."),

    # ── Family / attendant names ──
    ("P-GM3", "Her son Rajesh is asking about the discharge plan, BP is 128/80 now."),
    ("P-GS1", "Attendant Fatima signed the consent, patient given 1 g paracetamol."),

    # ── Hypothetical / planning ──
    ("P-GM3", "If she deteriorates like Mr Iyer did we should start noradrenaline."),
    ("P-CC1", "Planning to do what we did for Deepak Joshi — 40 mg IV frusemide."),

    # ── Hinglish / transliterated ordinary charting (room's own patient) ──
    ("P-GM3", "Patient ko bukhar hai, 101 F. Paracetamol 650 mg de diya."),
    ("P-GM3", "BP 130/84 hai, pulse 88. Theek lag rahi hai."),
    ("P-PU1", "Saans ki dikkat kam hai aaj, spo2 95% on 2L. Neb 6 hourly chal raha hai."),
    ("P-CC1", "Injection lasix 40 mg IV lagaya, urine output achha hai."),
    ("P-GM3", "Priya ka sugar 180 hai fasting, insulin 6 units diya."),

    # ── Devanagari (documented gap — must stay silent, must not crash) ──
    ("P-GM3", "मरीज को बुखार है, पैरासिटामोल 650 mg दिया।"),
    ("P-GM3", "रक्तचाप 130/80, नाड़ी 88 प्रति मिनट।"),

    # ── Acknowledgements and coordination ──
    ("P-GM3", "ok"),
    ("P-GM3", "Thanks, will do."),
    ("P-CC1", "On it — heading there now."),
    ("P-GM3", "Can someone bring the ECG machine to bed 103 please?"),
    ("P-GM3", "Ward round at 9, please have the obs charted before then."),
    ("P-CC1", "Bed 4 is free now if you need it for the new admission."),
    ("P-GM3", "Pharmacy says the stock will arrive by 4pm."),
    ("P-PU1", "Family updated, they're happy with the plan."),
    ("P-GS1", "Consent form signed and filed."),
    ("P-NE1", "MRI slot confirmed for tomorrow 11am."),
    ("P-OR4", "Physio referral sent."),
    ("P-GM3", "I'll chase the blood results."),
    ("P-CC1", "Cardiology reg has been bleeped."),
    ("P-GM3", "Handover done, going off shift."),
    ("P-GS1", "Theatre says they can take him at 2pm."),
    ("P-PU1", "Sputum sample sent to micro."),
    ("P-GM3", "Catheter bag changed, output 400 ml since midnight."),
    ("P-CC1", "Repeat ECG done, no new changes."),
    ("P-NE1", "Neuro obs half hourly as charted."),
    ("P-OR4", "Wound dressing changed, looks healthy."),
    ("P-GM3", "Diet advice given, dietician to review tomorrow."),
    ("P-GM4", "Fluids running at 100 ml/hr, next bag due at 6."),
    ("P-CC2", "Weight today 68.4 kg, down 1.2 kg from admission."),
    ("P-NE3", "Speech therapy assessment completed."),
    ("P-PU2", "Peak flow 320, improved from 280 this morning."),
    ("P-GS3", "NG tube position confirmed on x-ray."),
    ("P-OR2", "Hip x-ray shows good position of the implant."),
    ("P-GM5", "Warfarin held today, INR 4.2."),
    ("P-CC5", "Echo booked for Thursday."),
    ("P-NE5", "CT head reported — no acute change."),
]


# ══════════════════════════════════════════════════════════════════════════════
#  POSITIVES — genuine wrong-room documentation. Each MUST reach flag or review.
#  (room_patient_id, message, expected_suspect_patient_id or None if ambiguous)
# ══════════════════════════════════════════════════════════════════════════════
POSITIVES: list[tuple[str, str, str | None]] = [
    # ── The headline case: the surname collision, written with a full name ──
    ("P-GM3", "Siddharth Nair's post-op wound is oozing, BP 96/58, "
              "gave 500 ml Hartmann's stat.", "P-OR4"),
    ("P-OR4", "Priya Nair spiked to 39.2 and I've given paracetamol 1 g IV.", "P-GM3"),

    # ── Plain full-name wrong-room documentation ──
    ("P-GM3", "Vikram Singh's troponin came back at 0.9, cardiology informed.", "P-CC1"),
    ("P-CC1", "Lakshmi Devi's BP is 84/50, I've started 500 ml saline.", "P-GM5"),
    ("P-PU1", "Fatima Begum vomited twice overnight, gave ondansetron 4 mg IV.", "P-GS1"),
    ("P-NE1", "Meena Kumari desaturated to 86%, put her on 60% oxygen.", "P-OR1"),
    ("P-GS1", "Manoj Kulkarni had a seizure at 0330, lorazepam 4 mg IV given.", "P-NE4"),
    ("P-OR4", "Ganesh Pillai's ABG shows pH 7.21 and pCO2 68, calling ITU.", "P-PU3"),

    # ── Cued single-token identity ("Mr X", "X's", Hinglish possessive) ──
    ("P-GM3", "Mr Iyer's sugar is 24 mmol, gave 6 units of actrapid.", "P-GM2"),
    ("P-CC1", "Mrs Agarwal ka BP 78/44 hai, noradrenaline 0.1 start kiya.", "P-CC4"),
    ("P-PU1", "Malhotra's peak flow dropped to 150, gave back-to-back nebs.", "P-PU2"),

    # ── MRN as the identity anchor ──
    ("P-GM3", "MRN-CC003 troponin 1.8, started on heparin infusion.", "P-CC3"),

    # ── Bed reference resolving in-ward to a different patient ──
    ("P-GM3", "Bed 105 is hypotensive, 88/52, I've given 250 ml stat.", "P-GM5"),
    ("P-CC1", "CCU-04 desaturated to 84%, now on 15L non-rebreathe.", "P-CC4"),

    # ── Ambiguous surname where the room patient is NOT in the collision set ──
    # (Must fire, must NOT name a destination — two Nairs are visible.)
    ("P-CC1", "Mr Nair's drain output is 400 ml of frank blood, "
              "I've given 1 unit of packed cells.", None),
]


# ══════════════════════════════════════════════════════════════════════════════
#  MISSES — cases this design deliberately does not catch. Documented, tested,
#  and not to be "fixed" without re-measuring the false-positive rate.
# ══════════════════════════════════════════════════════════════════════════════
KNOWN_MISSES: list[tuple[str, str, str]] = [
    ("P-GM3", "BP 80/40, gave 500 ml stat and called the reg.",
     "no identity anchor of any kind — indistinguishable from ordinary charting"),
    ("P-GM3", "Nair's troponin is 0.9, cardiology aware.",
     "surname resolves to THIS room's patient; firing here would alarm on "
     "every legitimate use of the room patient's own name"),
    ("P-GM3", "सिद्धार्थ नायर का रक्तचाप 96/58 है।",
     "Devanagari name against a Latin roster — transliteration not attempted"),
]
