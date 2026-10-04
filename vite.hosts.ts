// Host names the Vite dev server answers to (server.allowedHosts).
//
// The dev server listens on every interface, so a web page on attacker.example
// can re-point its own name at this machine (DNS rebinding) and then read the
// app and call /api through the proxy. The backend's Host gate cannot see that:
// changeOrigin rewrites Host to localhost:8000 before the request reaches it.
// So the dev server needs its own list, and it is the same one the backend
// installs (server/config.py allowed_host_names()): localhost, the hosts in
// ORB_CORS_ORIGINS and ORB_PUBLIC_APP_BASE_URL, ORB_HOST when it is a name, this
// machine's own name with its mDNS .local form, and whatever ORB_ALLOWED_HOSTS
// adds. "*" in ORB_ALLOWED_HOSTS turns the check off, as it does on the backend.
//
// Vite itself always admits localhost, *.localhost and every IP address, so the
// LAN-IP case (run-orb.sh --dev --lan) needs nothing here. A tunnel such as ngrok
// does: list its name in ORB_ALLOWED_HOSTS.

export function devAllowedHosts(
  env: Record<string, string | undefined>,
  machineName: string,
): true | string[] {
  const extra = split(env.ORB_ALLOWED_HOSTS)
  if (extra.includes('*')) return true

  const names = new Set<string>(['localhost'])
  const sources = [
    ...split(env.ORB_CORS_ORIGINS).filter((origin) => origin !== '*'),
    env.ORB_PUBLIC_APP_BASE_URL ?? '',
    env.ORB_HOST ?? '',
    ...extra,
  ]
  for (const value of sources) {
    const name = hostName(value)
    if (name) names.add(name)
  }
  const own = hostName(machineName)
  if (own) {
    const label = own.split('.')[0]
    names.add(own).add(label).add(`${label}.local`)
  }
  return [...names].sort()
}

function split(value: string | undefined): string[] {
  return (value ?? '').split(',').map((part) => part.trim()).filter(Boolean)
}

// The host NAME in a URL or a bare host[:port], lowercased, trailing dot dropped.
// null for an IP address (Vite admits those anyway) or anything that is not a
// usable name.
function hostName(value: string): string | null {
  const text = value.trim()
  if (!text) return null
  let host: string
  try {
    host = new URL(text.includes('//') ? text : `http://${text}`).hostname
  } catch {
    return null
  }
  host = host.replace(/\.$/, '')
  if (host.startsWith('[') || /^\d{1,3}(\.\d{1,3}){3}$/.test(host)) return null
  return /^[a-z0-9_-]+(\.[a-z0-9_-]+)*$/.test(host) ? host : null
}
