"""User / auth API schemas (Pydantic v2).

Field names match every call site in routes/auth_routes.py and main.py.
"""

from pydantic import BaseModel, Field


class UserLogin(BaseModel):
    """Login request body. `employee_id` is the clinician employee ID or, for
    patients, their patient ID (see auth_routes.login)."""

    employee_id: str
    password: str
    # Optional TOTP code, required only when the account has MFA enabled. When a
    # correct password is supplied without a code for an MFA account, login
    # returns 401 with detail="mfa_required" so the client can prompt for it.
    totp_code: str | None = None


class PinLogin(BaseModel):
    """PIN sign-in on an enrolled ward tablet.

    Deliberately NOT a variant of UserLogin: the device credential is not in the
    body at all. It arrives as the HttpOnly `orb_device` cookie (or the
    X-Orb-Device-Token header on the one-time bind), so a PIN typed into any
    other browser carries nothing that could stand in for it.
    """

    employee_id: str
    pin: str


class DeviceEnrollBody(BaseModel):
    """Admin enrolling a tablet. `department` is the ward the tablet lives on —
    recorded for the device inventory; it never widens what a person signed in on
    it can see (permissions.department_scope is derived from the USER, always)."""

    label: str = Field(..., min_length=2, max_length=120)
    department: str | None = Field(default=None, max_length=120)


class DeviceBindBody(BaseModel):
    """The tablet exchanging its one-time secret for an HttpOnly device cookie."""

    device_secret: str = Field(..., min_length=16, max_length=256)


class DevicePinBody(BaseModel):
    """Setting one's own tablet PIN. The current password is required so a
    borrowed unlocked session cannot silently mint itself a tablet credential."""

    password: str
    pin: str = Field(..., min_length=4, max_length=8)


class AdminDevicePinBody(BaseModel):
    """An administrator provisioning a tablet PIN for a member of staff."""

    pin: str = Field(..., min_length=4, max_length=8)


class MfaEnrollBody(BaseModel):
    """Body for confirming MFA enrolment: the first live code from the app."""

    totp_code: str


class MfaDisableBody(BaseModel):
    """Body for disabling MFA: current password + a live code (defence in depth)."""

    password: str
    totp_code: str


class FacilityOut(BaseModel):
    """The facility an account works at (multisite-5a). `name` is the display
    name: facility 1 without a stored name reads the site settings file."""

    id: int
    code: str | None = None
    name: str | None = None


class UserOut(BaseModel):
    """Public-safe user representation returned to the client.

    `facility` (multisite-5a) is the facility the account works at
    (services/facility_service.user_facility): facility 1 while only one is on
    record; None for a department no facility owns. It is a label: what the
    account may see is decided by permissions.facility_scope on the server."""

    id: str
    name: str
    role: str
    department: str | None = None
    registration_number: str | None = None
    facility: FacilityOut | None = None


class TokenResponse(BaseModel):
    """Successful login response: JWT + the authenticated user."""

    token: str
    user: UserOut
