import { apiFetch } from './client'
import { workbenchDownload } from './localWorkbenchDownload'

export type ReportValue = string | number | boolean | null
export interface ReportFilter {
  field: string
  op: 'eq' | 'ne' | 'in' | 'contains' | 'gte' | 'lte' | 'is_null'
  value: ReportValue | ReportValue[]
}
export interface ReportQuery {
  dataset: string
  mode?: 'rows' | 'summary'
  columns?: string[]
  filters?: ReportFilter[]
  group_by?: string[]
  measures?: string[]
  limit?: number
  from_day?: string
  to_day?: string
}
export interface ReportDataset {
  id: string; label: string
  fields: { id: string; kind: 'text' | 'integer'; groupable: boolean }[]
  default_columns: string[]; measures: string[]
  date_required: boolean; cohort_problem_filter: boolean
}
export interface ReportCatalog {
  datasets: ReportDataset[]
  templates: { name: string; query: ReportQuery }[]
  limits: { filters: number; columns: number; groups: number; rows: number; days: number }
  notes: string[]
}
export interface ReportResult {
  dataset: string; mode: 'rows' | 'summary'; columns: string[]
  rows: Record<string, ReportValue>[]
  truncated: boolean; limit: number
  period: { from_day: string; to_day: string } | null
  scope: { department: string | null; facilities: number[] | null }
}
export interface ReportDefinition {
  id: string; name: string; version: number; created_at: string; updated_at: string
  query: ReportQuery
}
export const getReportCatalog = () => apiFetch<ReportCatalog>('/reporting/catalog')
export const runReport = (query: ReportQuery) => apiFetch<ReportResult>('/reporting/query', { method: 'POST', body: JSON.stringify(query) })
export const exportReport = (query: ReportQuery) => workbenchDownload('/reporting/query.csv', 'orb-report.csv', query)
export const getReportDefinitions = async () => {
  const response = await apiFetch<{ definitions: ReportDefinition[] }>('/reporting/definitions')
  return response.definitions
}
export const saveReportDefinition = (name: string, query: ReportQuery, existing?: Pick<ReportDefinition, 'id' | 'version'>) => apiFetch<Omit<ReportDefinition, 'query'>>(
  `/reporting/definitions${existing ? `/${encodeURIComponent(existing.id)}` : ''}`,
  { method: existing ? 'PUT' : 'POST', body: JSON.stringify({ name, query, ...(existing ? { version: existing.version } : {}) }) },
)
export const deleteReportDefinition = (id: string) => apiFetch<{ deleted: true }>(`/reporting/definitions/${encodeURIComponent(id)}`, { method: 'DELETE' })
