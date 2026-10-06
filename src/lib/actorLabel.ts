/**
 * "Name (id)" for a person in a log, the name read by the server at read time.
 *
 * The audit chain stores only the id (a renamed user must not rewrite
 * history), so the read routes add `actor_name` from users. A user who no
 * longer exists comes back with no name, and the id alone is shown. An actor
 * that is not a person (a system node, a role) has no name either.
 */
export function actorLabel(name?: string | null, id?: string | null): string {
  const who = (name ?? '').trim()
  const key = (id ?? '').trim()
  if (who && key) return `${who} (${key})`
  return who || key
}
