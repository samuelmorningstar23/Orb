import { useCallback, useEffect, useRef, useState } from 'react'

export function useOperations<R, D extends { id: string }>(loader: (offset: number) => Promise<R>, getter: (id: string) => Promise<D>) {
  const [data, setData] = useState<R | null>(null)
  const [selected, setSelected] = useState<D | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [offset, setOffset] = useState(0)
  const alive = useRef(false), selectedId = useRef(''), readVersion = useRef(0), listVersion = useRef(0), saving = useRef(false), scopeVersion = useRef(0)
  const select = useCallback(async (id: string) => {
    selectedId.current = id
    const request = ++readVersion.current
    setSelected(null)
    setError('')
    if (!id) return
    try {
      const result = await getter(id)
      if (alive.current && request === readVersion.current) setSelected(result)
    } catch (reason) {
      if (alive.current && request === readVersion.current) {
        setError(reason instanceof Error ? reason.message : 'Record could not be read.')
        if ([401, 403, 404].includes((reason as { status?: number })?.status || 0)) {
          ++listVersion.current; selectedId.current = ''; setData(null); setLoading(false)
        }
      }
    }
  }, [getter])
  const refresh = useCallback(() => {
    const request = ++listVersion.current
    return Promise.resolve().then(() => loader(offset)).then(async result => {
      if (alive.current && request === listVersion.current) {
        setData(result); setError('')
        if (selectedId.current) await select(selectedId.current)
      }
    }).catch((reason: unknown) => {
      if (alive.current && request === listVersion.current) {
        ++readVersion.current
        setData(null); setSelected(null)
        setError(reason instanceof Error ? reason.message : 'Register could not be read.')
      }
    }).finally(() => {
      if (alive.current && request === listVersion.current) setLoading(false)
    })
  }, [loader, select, offset])
  useEffect(() => {
    alive.current = true
    ++readVersion.current; ++scopeVersion.current
    void refresh()
    return () => { alive.current = false }
  }, [refresh])
  const run = async (operation: () => Promise<unknown>) => {
    if (saving.current) return false
    saving.current = true
    const owner = scopeVersion.current
    ++listVersion.current; ++readVersion.current; setLoading(false)
    setBusy(true); setError('')
    try {
      await operation()
      if (alive.current && owner === scopeVersion.current) await refresh()
      return alive.current && owner === scopeVersion.current
    } catch (reason) {
      if (alive.current && owner === scopeVersion.current) {
        setError(reason instanceof Error ? reason.message : 'The change could not be saved.')
        const status = (reason as { status?: number })?.status
        if (status === 401 || status === 403 || status === 404) {
          ++readVersion.current; ++listVersion.current; selectedId.current = ''; setSelected(null); setData(null); setLoading(false)
        }
      }
      return false
    } finally { saving.current = false; if (alive.current) setBusy(false) }
  }
  return { data, selected, error, loading, busy, select, refresh: async () => { setLoading(true); await refresh() }, run, offset, page: (next: number) => {
    if (saving.current) return
    selectedId.current = ''; ++readVersion.current; ++listVersion.current; setSelected(null); setData(null); setLoading(true); setOffset(Math.max(0, next))
    if (Math.max(0, next) === offset) void refresh()
  } }
}

export function useRequestKey() {
  const key = useRef(crypto.randomUUID())
  return { key, renew: () => { key.current = crypto.randomUUID() } }
}

