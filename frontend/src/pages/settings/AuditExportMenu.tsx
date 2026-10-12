import { useRef, useState } from 'react'
import { toast } from 'sonner'
import { Download, FileJson, FileSpreadsheet, Loader2, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  AUDIT_EXPORT_ROW_LIMIT,
  useExportAuditLogs,
  type AuditExportFormat,
  type AuditFilters,
} from '@/api/audit'
import { ApiError } from '@/api/types'
import { saveBlob } from '@/lib/download'

/** What each format the API produces actually contains (`export_audit_logs`). */
const FORMATS: { format: AuditExportFormat; label: string; detail: string; icon: LucideIcon }[] = [
  {
    format: 'csv',
    label: 'CSV',
    detail: 'For spreadsheets. When, action, actor and target — no field changes.',
    icon: FileSpreadsheet,
  },
  {
    format: 'json',
    label: 'JSON',
    detail: 'Every field of each entry, including before and after values.',
    icon: FileJson,
  },
]

function exportErrorMessage(err: unknown): string {
  const status = err instanceof ApiError ? err.status : undefined
  if (status === 403) return "You don't have permission to export the audit log."
  // A filter the server will not accept, in its own words (e.g. the one-year range).
  if (status === 400 || status === 422) return (err as ApiError).message
  return "Couldn't export the audit log. Please try again."
}

/**
 * Export the audit entries matching the filters on screen
 * (`GET /audit-logs/export`). The file is built by the server from the whole
 * matching trail, not from the page of rows in the table.
 *
 * Render it only for holders of `audit.export`; the server enforces that.
 */
export function AuditExportMenu({
  filters,
  matching,
}: {
  /** The filters the table is showing. */
  filters: AuditFilters
  /** How many entries match them, when the list has said. */
  matching?: number
}) {
  const [open, setOpen] = useState(false)
  const exporter = useExportAuditLogs()
  // `isPending` only disables the buttons after a re-render; this also stops a
  // second request fired before that happens.
  const busy = useRef(false)

  async function run(format: AuditExportFormat) {
    if (busy.current) return
    busy.current = true
    setOpen(false)
    try {
      const file = await exporter.mutateAsync({ format, filters })
      saveBlob(file.blob, file.filename)
      toast.success(`Downloaded ${file.filename}`)
    } catch (err) {
      toast.error(exportErrorMessage(err))
    } finally {
      busy.current = false
    }
  }

  const filtered = Object.values(filters).some((value) => value !== undefined)
  let scope: string
  if (matching === undefined) {
    scope = filtered ? 'Exports the entries matching the current filters.' : 'Exports the audit log.'
  } else if (matching === 0) {
    scope = 'No entries match the current filters, so the file will be empty.'
  } else {
    const count = `${matching.toLocaleString()} ${matching === 1 ? 'entry' : 'entries'}`
    scope = filtered ? `${count} match the current filters.` : `${count} in the audit log.`
  }
  const capped = matching !== undefined && matching > AUDIT_EXPORT_ROW_LIMIT

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" disabled={exporter.isPending} aria-busy={exporter.isPending}>
          {exporter.isPending ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
          {exporter.isPending ? 'Exporting…' : 'Export'}
        </Button>
      </PopoverTrigger>
      <PopoverContent aria-label="Export audit log" className="w-80 max-w-[calc(100vw-1.5rem)] space-y-3 p-4">
        <div className="space-y-1">
          <p className="font-display text-primary text-base font-bold">Export audit log</p>
          <p className="font-body text-on-surface-variant text-xs">{scope}</p>
          {capped && (
            <p className="font-body text-xs text-amber-700" role="note">
              One file holds at most {AUDIT_EXPORT_ROW_LIMIT.toLocaleString()} entries, so only the
              newest {AUDIT_EXPORT_ROW_LIMIT.toLocaleString()} are exported. Narrow the dates to get
              the rest.
            </p>
          )}
        </div>
        <ul className="space-y-2">
          {FORMATS.map(({ format, label, detail, icon: Icon }) => (
            <li key={format}>
              <button
                type="button"
                onClick={() => run(format)}
                className="neo-pressed bg-surface hover:bg-secondary/10 focus-visible:ring-secondary flex w-full items-start gap-3 rounded-xl px-3 py-2.5 text-left transition-colors outline-none focus-visible:ring-2"
              >
                <Icon className="text-secondary mt-0.5 size-4 shrink-0" />
                <span className="min-w-0">
                  <span className="font-body text-body-sm text-on-surface block font-semibold">
                    Download {label}
                  </span>
                  <span className="font-body text-on-surface-variant block text-xs">{detail}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
