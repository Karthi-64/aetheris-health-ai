import { useRef } from 'react'
import { toast } from 'sonner'
import { Download, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  apiErrorMessage,
  useExportReport,
  type ReportExportParams,
  type ReportExportPeriod,
  type ReportId,
} from '@/api/reports'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { saveBlob } from '@/lib/download'
import { REPORT_READ_CODES, holdsAny } from './reportSections'

const EXPORT_FAILED = "Couldn't export the report. Try again."

function exportErrorMessage(err: unknown): string {
  const status = err instanceof ApiError ? err.status : undefined
  if (status === 403) return "You don't have permission to export this report."
  // A filter or format the server will not accept, in its own words.
  if (status === 400 || status === 422) return apiErrorMessage(err, EXPORT_FAILED)
  return EXPORT_FAILED
}

/**
 * Download the report on screen as a CSV file (`GET /reports/{id}/export`).
 * The file is built by the server from the same filters the page is showing;
 * it holds the period table (or the invoice list), not the breakdowns.
 *
 * Shown only to a user who holds `report.export` and may read this report —
 * the server requires both.
 */
export function ReportExportButton({
  reportId,
  params,
  period,
  disabled = false,
}: {
  reportId: ReportId
  /** The filters in the address bar; exactly these are sent. */
  params?: ReportExportParams
  /** The period the server resolved, to name the file when its own name cannot be read. */
  period?: ReportExportPeriod
  /** True while the filters on screen are ones the server would refuse. */
  disabled?: boolean
}) {
  const { can } = usePermissions()
  const exporter = useExportReport()
  // `isPending` only disables the button after a re-render; this also stops a
  // second request fired before that happens.
  const busy = useRef(false)

  if (!can('report.export') || !holdsAny(can, REPORT_READ_CODES[reportId])) return null

  async function run() {
    if (busy.current) return
    busy.current = true
    try {
      const file = await exporter.mutateAsync({ reportId, params, period })
      saveBlob(file.blob, file.filename)
      toast.success(`Downloaded ${file.filename}`)
    } catch (err) {
      toast.error(exportErrorMessage(err))
    } finally {
      busy.current = false
    }
  }

  return (
    <Button
      variant="outline"
      size="sm"
      onClick={run}
      disabled={disabled || exporter.isPending}
      aria-busy={exporter.isPending}
    >
      {exporter.isPending ? (
        <Loader2 className="size-4 animate-spin" aria-hidden />
      ) : (
        <Download className="size-4" aria-hidden />
      )}
      {exporter.isPending ? 'Exporting…' : 'Export CSV'}
    </Button>
  )
}
