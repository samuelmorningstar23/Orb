/* The header's Sage status line, kept apart from AiStatusChip.tsx so that
   file exports only components (fast refresh). The parameter is the part of
   /health/models this line reads. */

/** What every role reads in the header: Sage's state in words. Model tags
 *  ("27B", "4B") meant nothing to a nurse and carried the state only as a dot
 *  colour; they stay in the administrator's popover. A tier an administrator
 *  unloaded is not "warming up": nothing reloads it in the background. */
export function sageStatusText(health: {
  ollama_reachable: boolean
  models: { warm: boolean; held?: boolean }[]
}): string {
  if (!health.ollama_reachable) return 'Sage offline'
  const cold = health.models.filter(m => !m.warm)
  if (cold.length === 0) return 'Sage ready'
  return cold.every(m => m.held) ? 'Sage limited' : 'Sage warming up'
}
