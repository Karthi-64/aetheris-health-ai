import { useMutation, useQuery } from '@tanstack/react-query'
import { http } from '@/api/http'
import { api } from '@/lib/api'
import { blobApiError, filenameFromContentDisposition } from '@/lib/download'

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

/** The formats `GET /audit-logs/export` produces (`?format=csv|json`). */
export type AuditExportFormat = 'csv' | 'json'

/** The most entries one export holds, newest first (`_EXPORT_ROW_LIMIT` in the audit router). */
export const AUDIT_EXPORT_ROW_LIMIT = 1000

export interface AuditExportFile {
  blob: Blob
  filename: string
}

/**
 * Fetch an export of the entries matching `filters` (requires `audit.export`).
 *
 * The response is the file itself, not an API envelope, so this uses the raw
 * Axios instance. The server names the file in `Content-Disposition`; where
 * that header is not readable (a cross-origin API does not expose it) a name
 * of the same shape is made up here.
 */
export async function fetchAuditExport(
  format: AuditExportFormat,
  filters: AuditFilters,
): Promise<AuditExportFile> {
  try {
    const res = await api.get<Blob>('/audit-logs/export', {
      params: {
        format,
        actor_id: filters.actor_id,
        action: filters.action,
        target_type: filters.target_type,
        from: filters.from,
        to: filters.to,
        q: filters.q,
      },
      responseType: 'blob',
    })
    const headers = res.headers as Record<string, unknown> | undefined
    const stamp = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-')
    return {
      blob: res.data,
      filename:
        filenameFromContentDisposition(headers?.['content-disposition']) ??
        `audit-logs-${stamp}.${format}`,
    }
  } catch (err) {
    throw await blobApiError(err)
  }
}

/** Export the audit trail and return the file for the caller to save. */
export function useExportAuditLogs() {
  return useMutation({
    mutationFn: ({ format, filters }: { format: AuditExportFormat; filters: AuditFilters }) =>
      fetchAuditExport(format, filters),
  })
}
