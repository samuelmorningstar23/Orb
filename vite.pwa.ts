import type { RuntimeCaching } from 'workbox-build'

/*
 * How the service worker answers a page load (a navigation).
 *
 * The server writes this computer's switches into every copy of the page it
 * serves (window.__ORB_SWITCHES__, server/main.py spa_switches), so the first
 * render already knows whether this is an Orb Ward Record computer (Samuel's
 * ruling, 9 October 2026: fix the startup flash).
 *
 * Workbox's default answers EVERY page load from the copy of index.html it
 * saved when the worker installed, without asking the server. That copy
 * carries the switches as they were on the day of the install, so a computer
 * switched to Orb Ward Record later kept drawing the full rail, Sage and the
 * score widgets on every reload until /api/health answered. Checked in a
 * browser on 9 October 2026: install on an ordinary site, restart the same
 * server as Ward Record, reload, and Orders, Messages, Sage, Scribe, Lens,
 * Forecast, Command, Simulator, Revenue and Appointments all reached the rail.
 *
 * So a page load asks the server first and the switches are the server's own
 * for this request. Only when the server does not answer within a few seconds
 * (the downtime page, a dead network) does the worker fall back to the last
 * page the server served, and, before any, to the copy saved at install, so
 * Orb still opens offline.
 */

/** No page navigation from the saved install copy: the server is asked first. */
export const ORB_NAVIGATE_FALLBACK = null

export const ORB_PAGE_CACHE = 'orb-pages'

/** Seconds to wait for the server before a page load falls back to a saved copy. */
export const ORB_PAGE_NETWORK_TIMEOUT_SECONDS = 3

/** A page load the server must answer first. Workbox writes this function's
 *  source into the service worker as it stands, so it refers to nothing
 *  outside itself. The API, uploads and the socket are not pages (the same
 *  prefixes the old navigateFallbackDenylist named). */
export function isOrbPageLoad({ request, url }: { request: Request; url: URL }): boolean {
  return request.mode === 'navigate' && !/^\/(api|uploads|socket\.io)/.test(url.pathname)
}

/** Every address is answered with the same page (index.html with the switches
 *  written in), so the saved copy is kept under one key: offline, any address,
 *  even one this browser never opened, gets the page the server served last,
 *  never the install copy once a newer one exists. Copied into the worker as
 *  source, like isOrbPageLoad. */
export const ORB_ONE_PAGE_KEY = {
  cacheKeyWillBeUsed: async ({ request }: { request: Request }) => new URL('/', request.url).href,
}

export const ORB_PAGE_CACHING: RuntimeCaching = {
  urlPattern: isOrbPageLoad,
  handler: 'NetworkFirst',
  options: {
    cacheName: ORB_PAGE_CACHE,
    networkTimeoutSeconds: ORB_PAGE_NETWORK_TIMEOUT_SECONDS,
    plugins: [ORB_ONE_PAGE_KEY],
    precacheFallback: { fallbackURL: 'index.html' },
  },
}
