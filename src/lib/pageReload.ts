/**
 * A full page reload, kept in one place so a test can see that one was asked
 * for (jsdom cannot navigate). Used after a sign-in moves to another facility
 * (multisite-19): every panel, poll and socket starts again under the new
 * session, and nothing the facility being left had on screen survives.
 */
export function reloadPage(): void {
  window.location.reload()
}
