import type { OfflineSnapshot } from '../hooks/useOfflineSnapshot'
import { useNow } from '../hooks/useNow'
import { newestCachedAt } from '../lib/offline/phiCache'
import { getUser } from '../api/client'
import './OfflineBanner.css'

/**
 * The staleness label. The most important twenty words on the screen.
 *
 * Orb caches PHI so a nurse in a corridor dead zone keeps her board. That is
 * only safe while she cannot possibly mistake the copy for the live record —
 * a cached NEWS2 read as current is worse than a blank screen, because a blank
 * screen sends her to find a connection and a stale one sends her onward.
 *
 * So this component is deliberately loud and deliberately specific. It does not
 * say "offline". It says WHEN the data on screen was true, in two forms (a
 * clock time she can compare against her watch, and an age in plain words), and
 * it says that nothing she does here will be recorded. `role="alert"` because
 * it is not ambient status: it changes what the reader is allowed to conclude
 * from everything below it.
 *
 * It renders nothing at all when the view is live. A banner that is always
 * present is a banner nobody reads.
 */

interface Props {
  /** The view's snapshot, from useOfflineSnapshot. */
  snapshot: OfflineSnapshot<unknown>
  /** What this view is showing, in the clinician's words: "shift board",
   *  "patient chart". Reads as "Showing the shift board saved at 14:32". */
  what: string
  /** Optional manual refetch. Worth offering when the connection may already be
   *  back but nothing has re-fetched yet — a nurse who has walked back to the
   *  station should not have to wait out a poll interval to trust her screen. */
  onRetry?: () => void
}

/** How long the age reading may drift on screen before it is re-rendered.
 *  "3 minutes old" that is really 9 minutes is its own small lie. */
const TICK_MS = 20_000

/**
 * Age in words. Minutes up to an hour, then hours and minutes — never a bare
 * decimal ("1.4 hours"), which reads as a measurement rather than as a warning,
 * and never a rounded-up "an hour ago" that could hide 90 minutes.
 */
function ageInWords(ms: number): string {
  if (ms < 60_000) return 'less than a minute old'
  const totalMinutes = Math.floor(ms / 60_000)
  if (totalMinutes < 60) {
    return `${totalMinutes} ${totalMinutes === 1 ? 'minute' : 'minutes'} old`
  }
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  const hourPart = `${hours} ${hours === 1 ? 'hour' : 'hours'}`
  if (minutes === 0) return `${hourPart} old`
  return `${hourPart} ${minutes} ${minutes === 1 ? 'minute' : 'minutes'} old`
}

function clockTime(savedAt: number): string {
  return new Date(savedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export default function OfflineBanner({ snapshot, what, onRetry }: Props) {
  const { source, savedAt, unreachable } = snapshot

  // Re-render on a slow tick so the age stays true while a nurse stands
  // looking at it. Only while degraded — a live view has nothing to age.
  // Connected, not failing and nothing yet is a first load in progress, which
  // useOfflineSnapshot returns as source 'none'. It is not offline: saying
  // Offline, not live there flashed the banner on every sign in and reload
  // until the first answer came back (seen in a browser, 9 October 2026).
  const loading = source === 'none' && snapshot.online && !unreachable
  const degraded = source !== 'live' && !loading
  const now = useNow(TICK_MS, degraded)

  if (!degraded) return null

  // "You are offline" and "Orb is not answering" are different problems with
  // different fixes (walk to the nurses' station vs. call IT), so they get
  // different words. `unreachable` means the tablet has a network and the
  // appliance did not answer.
  const heading = unreachable ? "Can't reach Orb, not live" : 'Offline, not live'

  // Fall back to the freshest thing this user has cached at all, so a view with
  // no copy of its own can still say when the device last heard from the server.
  const deviceNewest = source === 'none' ? newestCachedAt(getUser()?.id ?? '') : null

  return (
    <div className="offline-banner" role="alert" data-testid="offline-banner">
      <span className="offline-banner__dot" aria-hidden="true" />
      <div className="offline-banner__body">
        <strong className="offline-banner__heading">{heading}</strong>

        {source === 'cache' ? (
          <p className="offline-banner__line">
            {savedAt === null ? (
              <>
                Showing a saved copy of the {what}. When it was saved is{' '}
                <strong>unknown</strong>, so treat every value on this screen as out of date.
              </>
            ) : (
              <>
                Showing the {what} saved at <strong>{clockTime(savedAt)}</strong>,{' '}
                <strong>{ageInWords(now - savedAt)}</strong>. Anything that changed
                since then is not here.
              </>
            )}
          </p>
        ) : (
          <p className="offline-banner__line">
            There is <strong>no saved copy</strong> of the {what} on this device, so there is
            nothing to show.
            {deviceNewest !== null && (
              <> This device last heard from Orb at <strong>{clockTime(deviceNewest)}</strong>.</>
            )}
          </p>
        )}

        <p className="offline-banner__line offline-banner__line--write">
          Nothing can be recorded until you reconnect. Observations, doses, notes and
          handovers are refused outright. They are never held back to send later, so enter
          them again once you are back.
          {onRetry && (
            <>
              {' '}
              <button type="button" className="offline-banner__retry" onClick={onRetry}>
                Try again
              </button>
            </>
          )}
        </p>
      </div>
    </div>
  )
}
