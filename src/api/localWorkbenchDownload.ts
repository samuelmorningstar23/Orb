/** Session-bound downloads. CSV is held in memory only until the browser save. */
import { API_BASE, ApiIdentityChangedError, buildHeaders, getUser, handleResponseError } from './client'

export async function workbenchDownload(path: string, filename: string, body?: unknown): Promise<void> {
  const owner = getUser()?.id ?? null
  const assertOwner = () => {
    if ((getUser()?.id ?? null) !== owner) throw new ApiIdentityChangedError()
  }
  const method = body === undefined ? 'GET' : 'POST'
  const response = await fetch(`${API_BASE}${path}`, {
    method, credentials: 'include', headers: buildHeaders(method),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  assertOwner()
  if (!response.ok) await handleResponseError(response, owner)
  const blob = await response.blob()
  assertOwner()
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  try {
    link.href = url
    link.download = filename
    link.click()
  } finally {
    URL.revokeObjectURL(url)
  }
}
