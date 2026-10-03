/* Kept apart from SageEmptyState.tsx so that file exports only components
   (fast refresh). */

/**
 * The name Sage greets the user by. "Dr." only when the stored name carries
 * it ("Dr. Meera Sharma" reads "Dr. Sharma"); anyone else by first name
 * ("Priya Reddy" reads "Priya"). Sage used to call every user "Dr.", so a
 * nurse was greeted as a doctor. Empty when there is no name.
 */
export function greetingName(userName?: string): string {
  const parts = (userName || '').trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return ''
  if (/^dr\.?$/i.test(parts[0])) {
    return parts.length > 1 ? `Dr. ${parts[parts.length - 1]}` : ''
  }
  return parts[0]
}
