import { useEffect, useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { FileClock } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert } from '@/components/ui/alert'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useAuditLogs, type AuditLogEntry } from '@/api/audit'
import { usePermissions } from '@/hooks/usePermissions'
import { AuditExportMenu } from './AuditExportMenu'

const PAGE_SIZE = 10

/** Parse a `YYYY-MM-DD` picker value into its local-midnight instant.
 *
 * The date input yields a local calendar date, so the bound must be built in
 * the browser's own timezone. The old `${date}T00:00:00.000Z` treated it as
 * UTC midnight (PR #29 review finding 8): in India the window was shifted by
 * 5h30m, and before 05:30 local "From = today" was in the future and the
 * server rejected it with a 422.
 */
function localDayStart(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(y, m - 1, d).toISOString()
}

/** The exclusive next local midnight, minus 1ms so the day is inclusive. */
function localDayEnd(date: string): string {
  const [y, m, d] = date.split('-').map(Number)
  return new Date(new Date(y, m - 1, d + 1).getTime() - 1).toISOString()
}

function formatTimestamp(iso: string) {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** Union of before/after field names, flattened into a diff table. */
function diffRows(entry: AuditLogEntry): { field: string; before: string; after: string }[] {
  const before = entry.before ?? {}
  const after = entry.after ?? {}
  const fields = [...new Set([...Object.keys(before), ...Object.keys(after)])]
  return fields.map((field) => ({
    field,
    before: formatValue(before[field]),
    after: formatValue(after[field]),
  }))
}

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

/**
 * Audit search tab (module 12 §12): structured filters over the durable
 * trail, with a detail dialog showing the before/after diff. Export appears
 * only for holders of `audit.export`.
 */
export function AuditLogTab() {
  const { can } = usePermissions()
  const [q, setQ] = useState('')
  const [action, setAction] = useState('')
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [page, setPage] = useState(1)
  const [selected, setSelected] = useState<AuditLogEntry | null>(null)
  const [debouncedQ, setDebouncedQ] = useState('')

  // Server rejects free-text searches under 3 characters (§11), so shorter
  // terms are simply not sent rather than surfacing a 422 toast.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedQ(q.trim().length >= 3 ? q.trim() : ''), 300)
    return () => clearTimeout(id)
  }, [q])

  // Every filter change invalidates the page cursor, so each setter resets it
  // in the same render pass (UsersPage pattern) rather than in an effect.
  function changeQ(value: string) {
    setQ(value)
    setPage(1)
  }
  function changeAction(value: string) {
    setAction(value)
    setPage(1)
  }
  function changeFrom(value: string) {
    setFrom(value)
    setPage(1)
  }
  function changeTo(value: string) {
    setTo(value)
    setPage(1)
  }

  const filters = useMemo(
    () => ({
      q: debouncedQ || undefined,
      action: action.trim() || undefined,
      from: from ? localDayStart(from) : undefined,
      to: to ? localDayEnd(to) : undefined,
    }),
    [debouncedQ, action, from, to],
  )

  const { data, isLoading, isError, isPlaceholderData, refetch } = useAuditLogs(filters, page, PAGE_SIZE)
  const entries = data?.items ?? []
  const pageCount = data?.pagination.totalPages ?? 1

  const columns: ColumnDef<AuditLogEntry>[] = useMemo(
    () => [
      {
        accessorKey: 'created_at',
        header: 'When',
        cell: ({ row }) => (
          <span className="text-on-surface-variant whitespace-nowrap">
            {formatTimestamp(row.original.created_at)}
          </span>
        ),
      },
      {
        accessorKey: 'action',
        header: 'Action',
        cell: ({ row }) => <Badge variant="primary">{row.original.action}</Badge>,
      },
      {
        id: 'actor',
        header: 'Actor',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex min-w-40 flex-col">
            <span className="font-medium">{row.original.actor_name ?? 'System'}</span>
            {row.original.actor_email && (
              <span className="text-on-surface-variant text-xs">{row.original.actor_email}</span>
            )}
          </div>
        ),
      },
      {
        id: 'target',
        header: 'Target',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant text-xs">
            {row.original.target_type ?? '—'}
            {row.original.target_id ? ` · ${row.original.target_id.slice(0, 8)}` : ''}
          </span>
        ),
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex justify-end">
            <Button
              variant="ghost"
              size="sm"
              aria-label={`View ${row.original.action} by ${row.original.actor_name ?? 'System'} at ${formatTimestamp(row.original.created_at)}`}
              onClick={() => setSelected(row.original)}
            >
              View
            </Button>
          </div>
        ),
      },
    ],
    [],
  )

  // While a changed filter is still loading, `data` is the previous search's
  // page, so its total says nothing about what would be exported.
  const exportMenu = can('audit.export') && (
    <AuditExportMenu filters={filters} matching={isPlaceholderData ? undefined : data?.pagination.total} />
  )

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Audit log</CardTitle>
        <CardDescription>
          Who did what, when — the durable compliance trail. Filters are server-side; date
          ranges are limited to one year per search.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-4">
          <Field label="Search" hint="Action or target, ≥ 3 characters">
            {(p) => (
              <Input
                {...p}
                value={q}
                placeholder="user.invited…"
                onChange={(e) => changeQ(e.target.value)}
              />
            )}
          </Field>
          <Field label="Action" hint="Exact match">
            {(p) => (
              <Input
                {...p}
                value={action}
                placeholder="patient.created"
                onChange={(e) => changeAction(e.target.value)}
              />
            )}
          </Field>
          <Field label="From">
            {(p) => (
              <Input {...p} type="date" value={from} onChange={(e) => changeFrom(e.target.value)} />
            )}
          </Field>
          <Field label="To">
            {(p) => (
              <Input {...p} type="date" value={to} onChange={(e) => changeTo(e.target.value)} />
            )}
          </Field>
        </div>

        {isError ? (
          <Alert variant="error" title="Couldn't load the audit trail">
            The search was rejected or the server is unreachable.{' '}
            <button onClick={() => refetch()} className="text-secondary font-bold hover:underline">
              Retry
            </button>
          </Alert>
        ) : (
          <DataTable
            columns={columns}
            data={entries}
            isLoading={isLoading}
            pageSize={PAGE_SIZE}
            serverPagination={{ page, totalPages: pageCount, onPageChange: setPage }}
            toolbarRight={exportMenu}
            emptyState={
              <EmptyState
                icon={FileClock}
                title="No audit entries match"
                description={
                  debouncedQ || action || from || to
                    ? 'Try a wider date range or fewer filters.'
                    : 'Actions performed in this hospital will appear here.'
                }
              />
            }
          />
        )}
      </CardContent>

      <Dialog open={!!selected} onOpenChange={(next) => (next ? undefined : setSelected(null))}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{selected?.action ?? 'Audit entry'}</DialogTitle>
            <DialogDescription>
              {selected ? `${formatTimestamp(selected.created_at)} · by ${selected.actor_name ?? 'System'}` : ''}
            </DialogDescription>
          </DialogHeader>
          {selected && (
            <div className="space-y-4">
              {diffRows(selected).length > 0 ? (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-on-surface-variant border-b text-left">
                      <th className="py-2 pr-4 font-medium">Field</th>
                      <th className="py-2 pr-4 font-medium">Before</th>
                      <th className="py-2 font-medium">After</th>
                    </tr>
                  </thead>
                  <tbody>
                    {diffRows(selected).map((row) => (
                      <tr key={row.field} className="border-b last:border-0">
                        <td className="py-2 pr-4 font-semibold">{row.field}</td>
                        <td className="text-on-surface-variant py-2 pr-4">{row.before}</td>
                        <td className="py-2">{row.after}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : (
                <p className="text-on-surface-variant text-sm">No field-level changes recorded.</p>
              )}
              {selected.context && (
                <div>
                  <p className="mb-1 text-sm font-semibold">Context</p>
                  <pre className="bg-surface-container text-on-surface rounded-lg p-3 text-xs">
                    {JSON.stringify(selected.context, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </DialogContent>
      </Dialog>
    </Card>
  )
}
