/**
 * Tells this browser's other tabs that a sign-in moved to another facility
 * (multisite-19, ruling MS-R7 (a)).
 *
 * Every tab of a browser shares one sign-in cookie, so the moment one tab
 * switches facility the others are answered for the new facility too, while
 * their screens still belong to the one being left. AppRoot reloads every
 * signed-in tab on this signal. It does not rely on the facility in the stored
 * sign-in: a tab opened from a sign-in stored by an older build has none, and
 * could not tell that anything had moved.
 *
 * The value is a fresh nonce each time (a storage event fires only when the
 * value changes) and names nobody: no user id or facility is kept in storage.
 */
const SWITCH_SIGNAL_KEY = 'orb_facility_switch'

/** Called by the tab that switched, once the server has moved its sign-in. */
export function announceFacilitySwitch(): void {
  try {
    localStorage.setItem(
      SWITCH_SIGNAL_KEY,
      `${Date.now()}.${Math.random().toString(36).slice(2, 10)}`,
    )
  } catch {
    // Storage unavailable: other tabs still compare the stored sign-in's
    // facility, and their next request answers for the new session.
  }
}

/** True when a storage event is another tab's facility switch. */
export function isFacilitySwitchSignal(event: StorageEvent): boolean {
  return event.key === SWITCH_SIGNAL_KEY && event.newValue != null
}
