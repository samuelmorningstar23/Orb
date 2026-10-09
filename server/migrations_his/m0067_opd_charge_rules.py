"""Migration 67: the outpatient charge rules (opd-13, W7-A).

Which service code an outpatient visit is charged against, set by the site and
never guessed. One table, CONFIGURATION, no patient data:

  opd_charge_rules   one rule per row, of three kinds:
                       consultation  the code a clinic's consultation is
                                     charged against: a doctor's own code, else
                                     the department's, else the hospital-wide
                                     one (optionally for one visit type only).
                                     With no rule the front desk keeps charging
                                     OPD-CONSULT, exactly as before.
                       follow_up     a visit inside a rule's window after a
                                     completed visit in the same clinic is
                                     charged against the rule's code
                                     (OPD-FOLLOWUP by default). The window maps
                                     to a code the site prices, never to an
                                     implicit zero (ruling OPD-N6).
                       registration  the registration fee (REG-FEE by default)
                                     raised once, when the desk first registers
                                     a patient. No rule, no fee.

services/opd_charges_service reads and writes these rows; the rules apply only
while the 'opd' feature is on (the registration fee: while 'registration' is),
so with every switch off nothing reads this table.

THE ROW RULES, all CHECKs so no writer can break them:
  * a follow_up rule names its window (1 to 365 days) and only a follow_up rule
    does;
  * a registration rule is hospital-wide: no department, doctor or visit type;
  * a follow_up rule takes no visit type (the window decides, not the type the
    desk picked);
  * a rule names a doctor or a department, never both (a doctor's department is
    the doctor's own);
  * a rule is never deleted: it is retired, with who and why.
One live rule per kind and match (doctor, department, visit type), enforced by
a partial unique index, so resolution never has to choose between two.

Text ids (never integers), so an audit row's target_id is the stored id
exactly (the int-versus-TEXT audit chain trap). No foreign keys, like every
other hospital-system table. Stamps are UTC in the canonical spelling
services.clinical_time writes.

Idempotent: CREATE TABLE and CREATE INDEX IF NOT EXISTS only, one statement per
conn.execute (executescript would commit the runner's transaction).
"""
from migrations import Migration

RULE_KINDS: tuple = ("consultation", "follow_up", "registration")
VISIT_TYPES: tuple = ("new", "follow_up", "review", "procedure")
MAX_FOLLOW_UP_WINDOW_DAYS = 365

OPD_CHARGE_RULES_DDL = """CREATE TABLE IF NOT EXISTS opd_charge_rules (
    id TEXT PRIMARY KEY,
    rule_kind TEXT NOT NULL CHECK (rule_kind IN ('consultation', 'follow_up', 'registration')),
    department TEXT,
    doctor_id TEXT,
    visit_type TEXT CHECK (visit_type IS NULL OR visit_type IN (
        'new', 'follow_up', 'review', 'procedure')),
    service_code TEXT NOT NULL CHECK (length(trim(service_code)) > 0),
    follow_up_window_days INTEGER CHECK (follow_up_window_days IS NULL
        OR follow_up_window_days BETWEEN 1 AND 365),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    notes TEXT,
    created_by TEXT,
    created_at TEXT NOT NULL,
    retired_by TEXT,
    retired_at TEXT,
    retired_reason TEXT,
    CHECK ((rule_kind = 'follow_up') = (follow_up_window_days IS NOT NULL)),
    CHECK (rule_kind != 'registration'
           OR (department IS NULL AND doctor_id IS NULL AND visit_type IS NULL)),
    CHECK (rule_kind != 'follow_up' OR visit_type IS NULL),
    CHECK (doctor_id IS NULL OR department IS NULL),
    CHECK (is_active = 1 OR retired_at IS NOT NULL)
)"""

OPD_CHARGE_RULE_INDEXES: tuple = (
    # One live rule per kind and match. lower() on the department because the
    # service compares clinics case-insensitively; departments are stored in
    # the covering spelling (users.department), so this only catches a typo'd
    # case, which is exactly the duplicate it must refuse.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_opd_charge_rules_live ON opd_charge_rules("
    "rule_kind, COALESCE(doctor_id, ''), lower(COALESCE(department, '')), "
    "COALESCE(visit_type, '')) WHERE is_active = 1",
    "CREATE INDEX IF NOT EXISTS idx_opd_charge_rules_kind ON opd_charge_rules("
    "rule_kind, is_active)",
)


def apply(conn) -> None:
    conn.execute(OPD_CHARGE_RULES_DDL)
    for statement in OPD_CHARGE_RULE_INDEXES:
        conn.execute(statement)


MIGRATION = Migration(67, "opd_charge_rules", apply)
