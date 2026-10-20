"""How a ward round types a medicine order: the vocabulary half of Sage's order gate.

`sage_service.detect_mutation_intent` is the gate. It used to read an order only
when the verb came first ("give lasix 40 iv to bed 13"). Measured on 9 October
2026, it missed most of what a round actually types, and on a miss the model's
own prose reached the screen:

    bed 13 give lasix 40 iv        the bed comes first
    bed 12: lasix 40 iv stat       no verb at all, a prescription line
    lasix 40 iv bd for bed 12      the drug comes first
    metformin stop                 the verb comes last
    lasix 40 iv de do              Hinglish
    hb 6.2, 2 prbc                 a reading, then the order

This module holds what the gate needs to read those, and nothing that decides
sentence form: the question test in sage_service still runs first, so nothing
here is ever asked about a question. It is pure and DB-free like the gate (it
runs on the event loop for every Sage turn), and it imports nothing from
sage_service: the caller passes `is_drug`, which also knows the hospital's own
formulary.

THE PRESCRIPTION LINE IS A CLOSED VOCABULARY, and that is the whole safety
argument. A message is a line only when EVERY word in it is prescription
vocabulary (drug, number, unit, route, frequency, duration, diluent, who, order
verb). A status or a question about the same drug always brings a word a line
has no use for, and that one word stops it:

    bed 13 lasix 40 iv bd          a line: an order (Samuel's ruling 2)
    bed 13 is on lasix 40 iv bd    "is", "on"
    lasix 40 iv given at 8         "given"
    pip-taz 4.5 g q8h day 3        "day"
    kcl 20 mmol in 500 ns safe     "safe"
    augmentin iv dose              "dose" with nothing before it: a lookup

tests/test_order_gate_wording.py wraps every verbless order line in 52 report
and question phrasings and requires that none trips. Fuzzy spelling is
deliberately NOT here: "gave" is one letter from "give" and "sat" (saturation)
one from "stat", so a typo-tolerant gate reads reports as orders.
"""

import re

# ── Drugs a ward types that the international generic list does not reach ──
# Indian brands and ward shorthand. Every word here is also safe for the
# verb-led patterns in sage_service ("stop the ecosprin"): each names a therapy
# and nothing else on a ward. Words that also mean something else ("blood",
# "k", "ns") are in LINE_ONLY_DRUG_WORDS below.
WARD_DRUG_NAMES = frozenset(
    {
        "pcm",
        "pantop",
        "pantocid",
        "rantac",
        "ranitidine",
        "famotidine",
        "ondem",
        "emeset",
        "zofer",
        "perinorm",
        "domperidone",
        "dolo",
        "crocin",
        "calpol",
        "combiflam",
        "monocef",
        "taxim",
        "cefotaxime",
        "cefuroxime",
        "cefoperazone",
        "sulbactam",
        "magnex",
        "piptaz",
        "tazo",
        "zosyn",
        "amoxiclav",
        "clavam",
        "azithro",
        "azee",
        "azithral",
        "doxy",
        "mero",
        "meronem",
        "duolin",
        "levolin",
        "asthalin",
        "budecort",
        "foracort",
        "deriphyllin",
        "theophylline",
        "lactulose",
        "glargine",
        "lantus",
        "actrapid",
        "mixtard",
        "humulin",
        "novomix",
        "aspart",
        "lispro",
        "atorva",
        "rosuva",
        "atorvastatin",
        "rosuvastatin",
        "ecosprin",
        "clopi",
        "clopilet",
        "dytor",
        "torsemide",
        "torasemide",
        "spiro",
        "aldactone",
        "metolazone",
        "mannitol",
        "dexona",
        "decadron",
        "hydrocort",
        "solumedrol",
        "wysolone",
        "oseltamivir",
        "tamiflu",
        "acyclovir",
        "acivir",
        "amphotericin",
        "ambisome",
        "thiamine",
        "optineuron",
        "neurobion",
        "ksol",
        "potchlor",
        "prbc",
        "ffp",
        "sdp",
        "rdp",
        "cryo",
        "lmwh",
        "ufh",
        "enox",
        "fondaparinux",
        "warf",
        "acitrom",
        "ntg",
        "isosorbide",
        "sorbitrate",
        "nicardipine",
        "esmolol",
        "norad",
        "dobu",
        "atropine",
        "glycopyrrolate",
        "neostigmine",
        "midaz",
        "buprenorphine",
        "diclo",
        "voveran",
        "lignocaine",
        "lidocaine",
        "glimi",
        "levipil",
        "eptoin",
        "valparin",
        "ativan",
        "diazepam",
        "haloperidol",
        "olanzapine",
        "quetiapine",
        "esomeprazole",
        "rabeprazole",
        "sucralfate",
        "octreotide",
        "terlipressin",
        "txa",
        "ethamsylate",
        "ivig",
        "hbig",
        "immunoglobulin",
        "rifampicin",
        "steroids",
        "steroid",
        "albendazole",
        "carbimazole",
        "thyronorm",
        "eltroxin",
        "telmisartan",
        "glimepiride",
        "linezolid",
        "colistin",
        "amlo",
        "amlong",
        "amlodac",
        "metolar",
        "met-xl",
        "pan-d",
        "rabicip",
        "shelcal",
        "oha",
        "ohas",
        "insulins",
        "vitk",
        "ironsucrose",
        # sage_service._BRAND_FLOOR (the name matcher's "never a patient's name"
        # brands): a brand that is never a name is a drug, and a parity test keeps
        # every floor word in this list.
        "meftal",
        "zerodol",
        "cifran",
        "ciplox",
        "zifi",
        "storvas",
        "rosuvas",
        "razo",
        "omez",
        "glycomet",
        "monotrate",
        "nitrocontin",
        "cordarone",
        "tazact",
        "huminsulin",
        "lupisulin",
        "novorapid",
        # brands and classes the blind sets named (sets 3 and 4)
        "flagyl",
        "metrogyl",
        "telma",
        "avil",
        "orofer",
        "transfusion",
        "antihypertensives",
        "antiplatelets",
        "anticoagulants",
        "diuretics",
        "sedatives",
        "analgesics",
        "laxatives",
        "nsaids",
        "statins",
    }
)

# Words that name a therapy on a prescription line but are ordinary English, or a
# reading, elsewhere ("blood results", "k 3.1", "ns" as in not significant).
# The line reads them as drugs; the verb-led patterns never do.
LINE_ONLY_DRUG_WORDS = frozenset(
    {
        "k",
        "blood",
        "fluid",
        "fluids",
        "o2",
        "oxygen",
        "ns",
        "rl",
        "dns",
        "d5",
        "d10",
        "d25",
        "d50",
        "saline",
        "dextrose",
        "nacl",
        "ivf",
        "antibiotics",
        "abx",
        "iron",
        "pan",
        "platelets",
        "platelet",
        "prc",
        "magsulph",
        "slidingscale",
        "akt",
        "inh",
        "insulin",
        "albumin",
    }
)

#: Multi-word drug names the verb-led patterns should still see as one object.
WARD_DRUG_PHRASES = (
    r"pip(?:er(?:acillin)?)?[\s/-]*taz(?:o(?:bactam)?)?|mag(?:nesium)?\.?\s+sulph(?:ate)?|"
    r"hep(?:atitis)?\s+b\s+ig|iron\s+sucrose|vit(?:amin)?\s+k|packed\s+(?:red\s+)?cells?"
)

#: Blood products and fluids, objects only for the verbs that take them.
BLOOD_AND_FLUIDS = (
    r"prbc|prc|ffp|sdp|rdp|cryo|platelets?|packed\s+(?:red\s+)?cells?|blood|units?|pints?|"
    r"ns|rl|dns|d5|d10|d25|saline|dextrose|fluids?|ivf|bolus|drip|infusion"
)

#: Verbs a round uses that the original gate did not have. Each still needs an
#: object, and sage_service gives them a gap that stops at a comma, so "slow
#: down, what is the dose" is not an order. The ordinary phrases they open are
#: excluded by name: "up to date", "hang on", "run through", "run out".
WARD_VERBS = (
    r"transfuse|infuse|nebuli[sz]e|bolus|push|load(?:\s+with)?|hang(?!\s+on\b)|"
    r"cover\s+with|slow(?:\s+down)?|cut(?:\s+down)?|bump(?:\s+up)?|up(?!\s+to\b)|"
    r"go\s+(?:up|down)\s+on|step\s+(?:down|up)\s+to|top\s+up(?:\s+with)?|"
    r"run(?!\s+(?:me\s+)?through|\s+out)|skip"
)

#: "escalate" alone is a nurse's word for calling a doctor ("escalate to the
#: registrar if the antibiotics fail"), so it orders only straight into a drug.
ESCALATE = r"(?:de-?)?escalate\s+(?:abx\s+|antibiotics?\s+)?to\s+(?:inj\s+|iv\s+|oral\s+)?"

# ── Token classes for the prescription line ──
FORM = frozenset(
    {"inj", "tab", "tabs", "cap", "caps", "syp", "syr", "susp", "neb", "nebs", "oint", "gtt"}
)
ROUTE = frozenset(
    {
        "iv",
        "im",
        "sc",
        "po",
        "pr",
        "sl",
        "ng",
        "ngt",
        "oral",
        "orally",
        "intravenous",
        "intravenously",
        "subcut",
        "subcutaneous",
        "inhaled",
        "topical",
        "ivp",
        "infusion",
        "drip",
        "slow",
        "chew",
    }
)
FREQ = frozenset(
    {
        "od",
        "bd",
        "bid",
        "tds",
        "tid",
        "qid",
        "qds",
        "hs",
        "nocte",
        "mane",
        "daily",
        "hourly",
        "stat",
        "sos",
        "prn",
        "weekly",
        "asper",
        "bedtime",
    }
)
URGENT = frozenset({"stat", "now", "immediately", "asap", "urgently", "abhi", "turant"})
WHEN = frozenset(
    {
        "now",
        "immediately",
        "asap",
        "urgently",
        "urgent",
        "once",
        "twice",
        "thrice",
        "abhi",
        "turant",
        "tonight",
        "today",
        "tomorrow",
        "morning",
        "evening",
        "night",
        "overnight",
        "meals",
        "fromnow",
        "loading",
    }
)
UNIT = frozenset(
    {
        "mg",
        "g",
        "gm",
        "gms",
        "gram",
        "grams",
        "milligram",
        "milligrams",
        "microgram",
        "micrograms",
        "mcg",
        "ug",
        "units",
        "unit",
        "u",
        "iu",
        "mu",
        "ml",
        "l",
        "litre",
        "litres",
        "liter",
        "liters",
        "mmol",
        "meq",
        "pint",
        "pints",
        "amp",
        "amps",
        "ampoule",
        "vial",
        "vials",
        "puffs",
        "puff",
        "%",
        "mg/kg",
        "ml/hr",
        "ml/kg",
        "mcg/kg/min",
        "units/hr",
        "u/kg/hr",
        "mg/hr",
        "mcg/hr",
        "mcg/min",
        "mg/min",
        "ml/min",
    }
)
DURATION_UNIT = frozenset(
    {"min", "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours", "days", "week", "weeks"}
)
BLOOD = frozenset({"prbc", "prc", "ffp", "sdp", "rdp", "cryo", "platelets", "platelet"})
ACTION = frozenset(
    {
        "stop",
        "hold",
        "discontinue",
        "d/c",
        "dc",
        "omit",
        "withhold",
        "cancel",
        "start",
        "restart",
        "resume",
        "commence",
        "initiate",
        "add",
        "give",
        "prescribe",
        "order",
        "increase",
        "inc",
        "decrease",
        "dec",
        "reduce",
        "double",
        "halve",
        "wean",
        "taper",
        "titrate",
        "change",
        "switch",
        "convert",
        "escalate",
        "de-escalate",
        "deescalate",
        "bump",
        "push",
        "load",
        "transfuse",
        "infuse",
        "run",
        "hang",
        "nebulise",
        "nebulize",
        "cover",
        "replace",
        "stepdown",
        "topup",
        "makeit",
        "tobe",
        "up",
        "make",
        "bolus",
        "slow",
        "cut",
        "skip",
        "keep",
        "putup",
        "adjust",
    }
)
#: "she needs lasix 40 iv" is an order; "he really needs paracetamol for the
#: fever" is not. These count only next to a dose, a route or a frequency.
WEAK_ACTION = frozenset(
    {
        "needs",
        "need",
        "get",
        "have",
        "let",
        "should",
        "to",
        # "repeat lasix 40 iv" orders a drug; "repeat potassium at 6 pm"
        # orders a blood test, outside this gate (ruling of 7 September)
        "repeat",
    }
)
#: Hinglish: a verb stem is an order only with an imperative ending after it.
HINDI_STEM = frozenset(
    {
        "band",
        "bandh",
        "chalu",
        "shuru",
        "start",
        "kam",
        "badha",
        "ghata",
        "laga",
        "chadha",
        "chala",
        "rok",
        "hata",
        "de",
        "kar",
        "daal",
        "lagwa",
        "chadhwa",
        "dilwa",
        "mangwa",
        "skip",
    }
)
HINDI_END = frozenset(
    {"do", "karo", "kardo", "dijiye", "kijiye", "karein", "dena", "dein", "karna"}
)
HINDI_ONE = frozenset(
    {
        "lagao",
        "chadhao",
        "badhao",
        "roko",
        "hatao",
        "ghatao",
        "lagaiye",
        "chadhaiye",
        "chalao",
        "chalaiye",
        "dijiye",
        "karo",
        "kardo",
    }
)
WHO = frozenset(
    {
        "she",
        "he",
        "her",
        "him",
        "they",
        "them",
        "pt",
        "patient",
        "bed",
        "cot",
        "room",
        "isko",
        "usko",
        "unko",
        "inko",
        "mr",
        "mrs",
        "ms",
        "smt",
        "shri",
        "no",
        "baby",
    }
)
HONORIFIC = frozenset({"mr", "mrs", "ms", "smt", "shri"})
CONNECT = frozenset(
    {
        "then",
        "and",
        "&",
        "+",
        "with",
        "via",
        "at",
        "of",
        "a",
        "an",
        "the",
        "more",
        "another",
        "one",
        "each",
        "x",
        "to",
        "for",
        "please",
        "kindly",
        "also",
        "plus",
        "ko",
        "ka",
        "ki",
        "ke",
        "mein",
        "ghante",
        "ghanta",
        "wale",
        "wali",
        "wala",
        "divided",
        "slowly",
        "over",
        "in",
        "hai",
        "se",
        "between",
        "aur",
        "only",
        "as",
    }
)
#: Formulation suffixes a brand carries: "pantop dsr", "ondem md", "magnex forte".
FORMULATION = frozenset(
    {"sr", "xr", "er", "cr", "dsr", "md", "forte", "plus", "duo", "xl", "ds", "lsr", "dt", "mr"}
)
#: "stat dose of lasix" is an order; "augmentin iv dose" is a lookup.
DOSE_OPENERS = frozenset(
    {"stat", "loading", "another", "more", "one", "next", "first", "a", "single", "extra", "same"}
)
CONDITION = frozenset({"if", "unless", "till", "until"})
REASON = frozenset({"for", "because", "due"})
#: Words that make what follows an order line a question.
TAIL_VETO = frozenset(
    {
        "why",
        "how",
        "what",
        "which",
        "who",
        "kya",
        "ok",
        "okay",
        "safe",
        "safety",
        "fine",
        "enough",
        "vs",
        "or",
        "allowed",
        "contraindicated",
        "correct",
        "right",
        "too",
    }
)
#: Words that make a line a report: something already done, running or known.
REPORT = frozenset(
    {
        "given",
        "gave",
        "had",
        "got",
        "received",
        "running",
        "ran",
        "on",
        "done",
        "completed",
        "charted",
        "held",
        "stopped",
        "started",
        "was",
        "were",
        "is",
        "are",
        "has",
        "been",
        "already",
        "currently",
        "home",
        "refused",
        "allergic",
        "allergy",
        "not",
        "day",
        "diya",
        "tha",
        "thi",
        "mila",
        "last",
        "this",
        "ok",
        "okay",
        "why",
        "how",
        "what",
        "or",
        "vs",
        "continue",
        "continued",
        "cont",
        "same",
        "after",
        "before",
        "with",
        "without",
        "plus",
        "since",
        "chart",
        "stock",
        "enough",
        "much",
        "effects",
        "interactions",
        "gaya",
        "gayi",
        "raha",
        "rahi",
        "ordered",
        "when",
        "which",
        "who",
        "long",
        "placebo",
        "kya",
        "nahi",
        "level",
        "levels",
        "should",
        "could",
        "would",
        "can",
        "years",
        "yrs",
        "months",
        "taken",
        "taking",
        "takes",
        "using",
        "uses",
        "meds",
        "medications",
        "medicines",
        "regular",
        "list",
    }
)
#: The REPORT words that tell a drug's own state. A reason after an order may
#: hold the rest ("inj lorazepam 2 mg iv now, bed 11 is seizing"); one of these
#: makes the clause a status ("inj monocef 1 g iv bd, day 3", "tab ecosprin 75 od
#: - taken").
DRUG_STATE = REPORT - frozenset(
    {
        "is",
        "are",
        "was",
        "were",
        "has",
        "been",
        "with",
        "without",
        "after",
        "before",
        "plus",
        "when",
        "which",
        "who",
        "not",
        "should",
        "could",
        "would",
        "can",
        "nahi",
    }
)
NUMBER = re.compile(
    r"^(?:\d+(?:\.\d+)?|\d+-\d+-\d+|\d+/\d+|q\d+h|q\d+hrly|\d+hrly|x\d+|"
    r"[a-z]{1,4}-\d{1,4}|b\d{1,3}|ek|do|teen|char)$"
)

#: Longest message the line checks read. A prescription line is short; the
#: limit keeps a pasted 512 KB note from paying for them on the event loop.
LINE_MAX_TOKENS = 40
SCAN_MAX_CHARS = 600


def normalise(q: str) -> str:
    """One spelling for each prescription word. `q` is already lower-cased."""
    q = re.sub(r"\bpip(?:er(?:acillin)?)?[\s/-]*taz(?:o(?:bactam)?)?\b", "piptaz", q)
    q = re.sub(r"\bmag(?:nesium)?\.?\s*sul(?:ph|f)(?:ate)?\b", "magsulph", q)
    q = re.sub(r"\bput\s+up\b", "putup", q)
    q = re.sub(r"\bnormal\s+saline\b", "ns", q)
    q = re.sub(r"\bringer'?s?\s+lactate\b", "rl", q)
    q = re.sub(r"\bwhole\s+blood\b", "blood", q)
    q = re.sub(r"\bpacked\s+(?:red\s+)?cells?\b", "prbc", q)
    q = re.sub(r"\bhep(?:atitis)?\s+b\s+ig\b", "hbig", q)
    q = re.sub(r"\bvit(?:amin)?\.?\s+k\b", "vitk", q)
    q = re.sub(r"\biron\s+sucrose\b", "ironsucrose", q)
    q = re.sub(r"\bsliding\s+scale\b", "slidingscale", q)
    q = re.sub(r"\bas\s+per\s+(?:slidingscale|protocol|scale)\b", "asper", q)
    q = re.sub(
        r"\b(?:before|after|with)\s+(?:each\s+)?(?:meals?|food|breakfast|lunch|dinner)\b",
        "meals",
        q,
    )
    q = re.sub(r"\b(?:on\s+)?(?:an\s+)?empty\s+stomach\b", "meals", q)
    q = re.sub(r"\bat\s+bedtime\b", "bedtime", q)
    q = re.sub(r"\b(?:once|twice|thrice)\s+(?:a\s+)?daily\b", "od", q)
    q = re.sub(r"\bfrom\s+(?:tomorrow|tmrw|today|tonight|now|the\s+next\s+dose)\b", "fromnow", q)
    q = re.sub(r"\bi\.v\.?(?=\s|$)", "iv", q)
    q = re.sub(r"\bi\.m\.?(?=\s|$)", "im", q)
    q = re.sub(r"\bs[./]c\.?(?=\s|$)", "sc", q)
    q = re.sub(r"\bp\.o\.?(?=\s|$)", "po", q)
    q = re.sub(r"\b(inj|tab|cap|syp)\.", r"\1 ", q)
    q = re.sub(r"\bstep\s+(?:down|up)\b", "stepdown", q)
    q = re.sub(r"\btop\s+up\b", "topup", q)
    q = re.sub(r"\bmake\s+(?:it|the)\b", "makeit", q)
    q = re.sub(
        r"\b(?:to|needs?\s+to)\s+be\s+(?:stopped|held|given|started|increased|reduced|"
        r"discontinued|omitted|withheld|restarted|changed|switched|tapered|weaned|"
        r"transfused|infused|administered|nebuli[sz]ed)\b",
        "tobe",
        q,
    )
    q = re.sub(r"\bkar\s+do\b", "kardo", q)
    q = re.sub(r"(?:->|→|=>)", " to ", q)
    q = q.replace("@", " at ")
    q = re.sub(r"(\d)\s*%", r"\1 %", q)
    q = re.sub(r"(\d)\s*(?:ml\s*)?(?:/|per\s+)(?:hour|hr|h)\b", r"\1 ml/hr", q)
    q = re.sub(r"(\d)\s*(?:u|units?)\s*/\s*(?:hour|hr|h)\b", r"\1 units/hr", q)
    q = re.sub(r"(\d)(mg|g|gm|mcg|ml|l|mmol|meq|u|iu|units?)\b", r"\1 \2", q)
    q = re.sub(
        r"(\d)\s*(mg/kg|units/hr|u/kg/hr|mg/hr|mcg/hr|ml/kg|mcg/min|mg/min|ml/min)\b", r"\1 \2", q
    )
    q = re.sub(r"\bx\s*(\d)", r"x\1", q)
    q = re.sub(r"'s\b", "", q)
    return q


def tokens(q: str) -> list:
    raw = re.split(r"[\s,;:()\[\]]+|(?<=\s)-(?=\s)|^-|-$", q)
    return [t.strip(".!?") for t in raw if t and t not in {"-", "."} and t.strip(".!?")]


def line_signals(q: str, is_drug):
    """The signals a prescription line carries, or None when `q` is not one."""
    toks = tokens(normalise(q))
    n = len(toks)
    if n < 2 or n > LINE_MAX_TOKENS:
        return None
    drugs = unknown = 0
    has = dict.fromkeys(
        ("route", "freq", "unit", "dur", "act", "weak", "form", "when", "urgent", "num"), False
    )
    prev = None

    def complete():
        return drugs and (
            has["act"]
            or has["urgent"]
            or (has["form"] and (has["unit"] or has["route"] or has["freq"]))
        )

    def met():
        return drugs and (has["act"] or has["urgent"] or has["unit"] or has["route"] or has["freq"])

    i = 0
    while i < n:
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < n else None
        # A condition or a reason after a complete order ("hold amlodipine if sbp
        # below 100", "sos inj emeset 4 mg iv for vomiting") ends the line, as long
        # as what follows asks or reports nothing.
        if unknown == 0 and (
            (t in CONDITION and met())
            or (t in REASON and met())
            or (t in ("before", "after", "then") and complete())
        ):
            veto = TAIL_VETO if t in CONDITION else (REPORT - {"before", "after"}) | TAIL_VETO
            if any(x in veto for x in toks[i:]):
                return None
            break
        if t in HINDI_STEM and nxt in HINDI_END:
            has["act"] = True
            i += 2
            if i < n and toks[i] == "hai" and toks[i - 1] == "dena":
                i += 1
            prev = "act"
            continue
        if t in URGENT:
            has["urgent"] = True
        if t in HINDI_ONE or (t == "dena" and nxt in (None, "hai", "abhi", "please")):
            has["act"] = True
        elif t in BLOOD and prev == "num":
            has["unit"] = True  # "2 prbc": the count of units is the dose
            drugs += 1
        elif is_drug(t):
            drugs += 1
        elif t == "bolus":
            has["act"] = has["route"] = True
        elif t in FORM:
            has["form"] = True
        elif t in ROUTE:
            has["route"] = True
        elif t in FREQ:
            has["freq"] = True
        elif t in WHEN:
            has["when"] = True
        elif t == "to" and nxt is not None and NUMBER.match(nxt) and drugs:
            has["act"] = True  # "lasix to 80", "lasix 40 -> 80"
        elif t in HINDI_STEM:
            pass  # a stem without an imperative ending orders nothing
        elif t in UNIT and prev == "num":
            has["unit"] = True
        elif t in DURATION_UNIT and prev == "num":
            has["dur"] = True
        elif t in ("am", "pm") and prev == "num":
            has["when"] = True
        elif NUMBER.match(t):
            if t.startswith("q") or t.endswith("hrly"):
                has["freq"] = True
            has["num"] = True
            prev = "num"
            i += 1
            continue
        elif t in ("dose", "doses") and (prev in DOSE_OPENERS or nxt == "of"):
            pass
        elif t in ACTION:
            has["act"] = True
        elif t in WEAK_ACTION:
            has["weak"] = True
        elif t in WHO or t in CONNECT:
            pass
        elif t in FORMULATION and prev == "drug":
            i += 1
            continue  # "pantop dsr": still the drug's name
        elif t == "this" and nxt in ("patient", "pt"):
            pass
        elif t in REPORT or t in TAIL_VETO:
            return None  # one report or question word and it is not a line
        elif prev == "honorific" and unknown == 0:
            unknown = -1  # the name after Mr or Mrs
        elif (
            t not in REPORT
            and t.isalpha()
            and len(t) >= 4
            and nxt is not None
            and is_drug(nxt)
            and unknown == 0
            and prev == "form"
        ):
            # One word of a drug's name, after inj/tab: "inj human mixtard". Not
            # at the start, where a misspelt verb would pass ("contine ns").
            unknown = -1
        else:
            unknown += 1 if unknown >= 0 else 2
        prev = (
            t
            if t in DOSE_OPENERS
            else "honorific"
            if t in HONORIFIC
            else "form"
            if t in FORM
            else "drug"
            if is_drug(t)
            else "other"
        )
        i += 1
    if unknown > 0:
        # An unknown word may only be the drug's own name (a brand the list does
        # not know), and only on a line that is unmistakably a prescription.
        if unknown > 2 or drugs:
            return None
        signature = (
            has["form"]
            or (has["unit"] and (has["route"] or has["freq"]))
            or (has["route"] and has["freq"])
            or (has["act"] and has["num"] and (has["unit"] or has["freq"] or has["route"]))
        )
        if not signature:
            return None
        drugs = 1
    if not drugs:
        # "make it 80 bd": a dose change whose drug the round already named.
        if "makeit" in toks and has["num"] and (has["freq"] or has["unit"]) and unknown <= 0:
            return has
        return None
    if (
        has["act"]
        or has["route"]
        or has["freq"]
        or has["unit"]
        or has["dur"]
        or has["urgent"]
        or (has["form"] and has["when"])
    ):
        return has
    if has["weak"] and (has["unit"] or has["route"] or has["freq"]):
        return has
    return None


def reading_then_order(q: str, is_drug) -> bool:
    """An order next to a reading or a reason, in either order.

    "bp 80/50, ns bolus 500", "hb 6.2, 2 prbc": a reading, then the order. A
    later clause counts when it is itself a line with an order verb or "stat"
    ("vomiting since morning, inj ondem 4 iv stat"); a bare last line counts only
    when nothing in the message reports and no earlier clause names a drug, which
    is what keeps "home meds: metformin 500 bd, amlodipine 5 od" a read.

    "inj morphine 4 mg iv stat, chest pain", "aldactone 25 od, add it": the
    order, then the reason. The first clause must be a complete order (a verb,
    "stat", or a form word with its dose) or the rest must hold an order verb,
    and nothing after it may report or ask ("inj monocef 1 g iv bd, day 3").
    """
    # Normalised first, so "i.v. stat" is not split at its full stop.
    parts = [
        p.strip() for p in re.split(r"[,;]|\s[-\u2013\u2014]\s|\.\s", normalise(q)) if p.strip()
    ]
    if len(parts) < 2:
        return False
    for part in parts[1:]:
        sig = line_signals(part, is_drug)
        if sig and (sig["act"] or sig["urgent"]):
            return True
    first = line_signals(parts[0], is_drug)
    if first:
        rest = [t for p in parts[1:] for t in tokens(normalise(p))]
        if not any(t in DRUG_STATE or t in TAIL_VETO for t in rest) and (
            first["act"]
            or first["urgent"]
            or (first["form"] and (first["unit"] or first["route"] or first["freq"]))
            or any(t in ACTION for t in rest)
        ):
            return True
    words = tokens(normalise(q))
    if any(t in REPORT or t in TAIL_VETO for t in words):
        return False
    for part in parts[:-1]:
        if line_signals(part, is_drug) or any(is_drug(t) for t in tokens(normalise(part))):
            return False
    return line_signals(parts[-1], is_drug) is not None


# ── Hinglish ──
_HI_IMPERATIVE = re.compile(
    r"\b(?:de|kar|laga|chadha|chala|rok|hata|badha|ghata|band|bandh|chalu|shuru|start|stop|"
    r"hold|daal|dal|increase|reduce|taper|wean|lagwa|chadhwa|dilwa|mangwa|skip)"
    r"\s+(?:do|dijiye|dena|karo|kardo|dein|karna)\b"
    r"|\b(?:kardo|chalao|chadhao|lagao|badhao|ghatao|roko|hatao|daalo|karo|dijiye|lagaiye|"
    r"chadhaiye|chalaiye|kijiye)\b"
    r"|\b(?:nahi|mat|na)\s+(?:dena|do|dein|lagana|chadhana)\b"
    # obligation: "sdp abhi chadhana hai", "lasix dena hai", "subah khali pet dena"
    r"|\b(?:dena|chadhana|lagana|chalana|karna)\s+hai\b|\bdena\s*$"
)
#: Asking, or telling what already happened: "diya kya?", "kitna ns chala",
#: "subah pcm de diya tha", "dena chahiye?".
_HI_NOT_AN_ORDER = re.compile(
    r"\b(?:kya|kab|kitna|kitni|kitne|kaun|kyun|kyon|kaise|diya|kiya|lagaya|lagayi|chadhaya|"
    r"chadhayi|mila|mili|tha|thi|gaya|gayi|chahiye|sakte|sakta|sakti)\b|\bki\s+nahi\b"
)
_HI_THERAPY = re.compile(r"\b(?:dawai|dawa|injection|drip|bottle|platelets?|blood|insulin)\b")


def hinglish_order(q: str, is_drug) -> bool:
    """A Hinglish order: an imperative and a therapy in the same clause, and
    nothing in that clause asking or telling the past. Per clause, because the
    reason after an order is often in the past: "metformin band karo,
    creatinine badh gaya"."""
    for clause in (c.strip() for c in re.split(r"[,;.]", q)):
        if not clause or not _HI_IMPERATIVE.search(clause) or _HI_NOT_AN_ORDER.search(clause):
            continue
        if any(is_drug(t) for t in tokens(normalise(clause))) or _HI_THERAPY.search(clause):
            return True
    return False


# ── Who the order is for, when it comes first ──
#: "bed 13", "13", "GM-512", "bed 13:", "pt in bed 12", "bed 13 ko", "mrs devi",
#: "her:", "sister,". Set aside so the verb-led patterns read what follows as the
#: start of the order.
PATIENT_REFERENCE = re.compile(
    r"^(?:(?:the\s+)?(?:pt|patient|lady|man|gentleman|boy|girl|baby)\s+(?:in|on|at)\s+)?"
    r"(?:(?:icu|micu|sicu|hdu|ccu|nicu|picu|ward|er|casualty|labour\s+room|lr)\s+)?"
    r"(?:(?:bed|cot|room|rm|pt|patient|case|b|bd)\s*(?:no\.?|number|#)?\s*[-.#:]?\s*)?"
    r"(?:[a-z]{1,4}-)?\d{1,4}[a-z]?"
    r"(?:'s)?(?:\s+(?:ko|ka|ki|ke|wale\s+ko|wali\s+ko|wala|wali|wale))?"
    r"\s*[:,\-\u2013\u2014)]*\s+"
    r"|^(?:mr|mrs|ms|smt|shri)\.?\s+[a-z]+\s*[:,\-\u2013\u2014]*\s+"
    r"|^(?:her|him|she|he)\s*[:,\-\u2013\u2014]+\s*"
    r"|^(?:sister|sis|staff|nurse|bhaiya|didi|brother)\b\s*[:,\-\u2013\u2014]*\s*"
)


_REQUEST_PREAMBLE = re.compile(
    r"^(?:(?:please|kindly|just)\s+"
    r"|(?:can|could|will|would)\s+(?:you|u)\s+(?:please\s+|kindly\s+)?"
    r"|(?:i|we)\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+"
    r"|go\s+ahead\s+and\s+)+"
)


def without_request(q: str) -> str:
    """ "can you please increase the ns to 120 ml/hr?" read as "increase the ns to
    120 ml/hr". The question test has already ruled that a polite message is an
    order (ruling 3), so its preamble and its "?" are not words of the order;
    left in, "can" and "could" read as a question to the line reader."""
    m = _REQUEST_PREAMBLE.match(q)
    rest = q[m.end() :] if m else q
    return rest.rstrip(" ?") if rest != q or q.endswith("?") else q


def after_patient_reference(q: str) -> str:
    """What follows a leading patient reference, or "" when there is none."""
    m = PATIENT_REFERENCE.match(q)
    return q[m.end() :] if m else ""


#: "Plan:", "plan -", "Rx:", "Orders:". Decided 20 October 2026: on a ward the
#: plan is the day's order list, so a plan line that names a medicine action is
#: read as the order under it ("Plan: inj ceftriaxone 2 g iv od"). Only a header
#: followed by a colon or a dash: "plan to start insulin" and "the plan is ..."
#: are prose, and a plan with no medicine action reads nothing the line readers
#: accept ("Plan: review in the evening").
PLAN_HEADER = re.compile(r"^(?:plan|rx|orders?)\s*(?:for\s+today\s*)?[:\-–—]\s*")


def after_plan_header(q: str) -> str:
    """What follows a leading plan header, or "" when there is none."""
    m = PLAN_HEADER.match(q)
    return q[m.end() :] if m else ""


# ── Spelling the gate repairs before it reads anything ──
_GLUED_VERB = re.compile(r"\b(give|start|stop|hold|add)([a-z]{4,})\b")


def repair(q: str, is_drug) -> str:
    """ "pls" is "please" (so a polite order stays polite), "inj.lasix40mg" is
    "inj lasix 40mg", "givelasix" is "give lasix". Nothing here guesses a word."""
    q = re.sub(r"\b(?:pls|plz|plss|pl)\b", "please", q)
    # "start inj. ceftriaxone": the stop after "inj" would end the clause.
    q = re.sub(r"\b(inj|tab|tabs|cap|caps|syp|syr|susp)\.\s*", r"\1 ", q)
    q = re.sub(r"\b([a-z]{3,})(\d)", r"\1 \2", q)
    return _GLUED_VERB.sub(
        lambda m: f"{m.group(1)} {m.group(2)}" if is_drug(m.group(2)) else m.group(0), q
    )
