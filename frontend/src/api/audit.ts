import { useQuery } from '@tanstack/react-query'
import { http } from '@/api/http'
import { api } from '@/lib/api'

/** One audit entry as returned by GET /api/v1/audit-logs. */
export interface AuditLogEntry {
  id: string
  action: string
  actor_id: string | null
  actor_name: string | null
  actor_email: string | null
  actor_type: string
  target_type: string | null
  target_id: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  context: Record<string, unknown> | null
  created_at: string
}

/** Structured filters for the audit search page (module 12 §9). */
export interface AuditFilters {
  actor_id?: string
  action?: string
  target_type?: string
  from?: string
  to?: string
  q?: string
}

export const auditKeys = {
  all: ['audit'] as const,
  list: (filters: AuditFilters, page: number, pageSize: number) =>
    [...auditKeys.all, 'list', { filters, page, pageSize }] as const,
}

/** Search the audit trail (requires audit.read). */
export function useAuditLogs(filters: AuditFilters, page: number, pageSize: number) {
  return useQuery({
    queryKey: auditKeys.list(filters, page, pageSize),
    queryFn: () =>
      http.getPaginated<AuditLogEntry>('/audit-logs', {
        params: {
          actor_id: filters.actor_id,
          action: filters.action,
          target_type: filters.target_type,
          from: filters.from,
          to: filters.to,
          q: filters.q,
          page,
          page_size: pageSize,
        },
      }),
    placeholderData: (prev) => prev,
    staleTime: 15_000,
  })
}

/**
 * Download an export file (requires audit.export). Uses the raw axios
 * instance — the response is a file, not an API envelope.
 */
export async function exportAuditLogs(
  format: 'csv' | 'json',
  filters: AuditFilters,
): Promise<void> {
  const res = await api.get('/audit-logs/export', {
    params: {
      format,
      actor_id: filters.actor_id,
      action: filters.action,
      target_type: filters.target_type,
      from: filters.from,
      to: filters.to,
      q: filters.q,
    },
    responseType: 'text',
  })
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
  const blob = new Blob([res.data as string], {
    type: format === 'csv' ? 'text/csv' : 'application/json',
  })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = `audit-logs-${stamp}.${format}`
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}
