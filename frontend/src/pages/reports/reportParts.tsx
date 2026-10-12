import type { ReactNode } from 'react'
import { RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { apiErrorMessage, type ReportMeta } from '@/api/reports'
import { ApiError } from '@/api/types'
import { cn } from '@/lib/utils'
import { ReportsTabs } from './ReportsTabs'

/** Shown in place of a report to a user its endpoint would refuse. No request is made. */
export function ReportDenied() {
  return (
    <div className="w-full">
      <ReportsTabs />
      <Alert variant="error" title="You can't view this report.">
        Your account is not permitted to see it. If that has just changed, sign in again.
      </Alert>
    </div>
  )
}

/**
 * A report that could not be read. A refusal the server wrote for the user —
 * a filter it will not accept, a missing permission — is shown in its words;
 * anything else gets a plain sentence, never internal text.
 */
export function ReportError({
  error,
  onRetry,
  onClearFilters,
}: {
  error: unknown
  onRetry: () => void
  /** Offered when filters are set: a refused filter is fixed by removing it. */
  onClearFilters?: () => void
}) {
  const refusedFilter = error instanceof ApiError && error.status === 422
  return (
    <Alert
      variant="error"
      title={refusedFilter ? "These filters can't be used" : "Couldn't load the report"}
    >
      <div className="flex flex-col items-start gap-3">
        <p>{apiErrorMessage(error, 'The report could not be reached. Check your connection and try again.')}</p>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw className="size-4" aria-hidden /> Retry
          </Button>
          {onClearFilters && (
            <Button variant="outline" size="sm" onClick={onClearFilters}>
              Clear filters
            </Button>
          )}
        </div>
      </div>
    </Alert>
  )
}

/** What the figures are measured in, from the report itself — not from the browser. */
export function ReportMetaLine({
  meta,
  withCurrency = false,
  lead,
}: {
  meta: ReportMeta | undefined
  /** Only on a report that shows money. */
  withCurrency?: boolean
  /** Said first, e.g. "As of 6 Oct 2026". */
  lead?: string
}) {
  if (!meta) return null
  return (
    <p className="font-body text-body-sm text-on-surface-variant mb-4 [overflow-wrap:anywhere]">
      {lead && <>{lead} · </>}
      All figures in {meta.timezone}
      {withCurrency && <> · {meta.currency}</>}
    </p>
  )
}

/** The row of headline figures above a report's chart. */
export function KpiGrid({ children }: { children: ReactNode }) {
  return <div className="mb-6 grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">{children}</div>
}

/** The heading of a table that follows the chart. */
export function SectionHeading({ children }: { children: ReactNode }) {
  return <h2 className="font-display text-title-lg text-primary mt-8 mb-3 font-bold">{children}</h2>
}

/** The first cell of a period table: the period, and whether the range cut it short. */
export function PeriodCell({ label, partial, isTotal }: { label: string; partial: boolean; isTotal: boolean }) {
  return (
    <span className={cn('flex flex-wrap items-center gap-2 whitespace-nowrap', isTotal && 'font-bold')}>
      {label}
      {partial && (
        <Badge variant="warning" title="The selected dates cover only part of this period.">
          partial
        </Badge>
      )}
    </span>
  )
}

/** A figure in a table, as the server sent it. */
export function FigureCell({ strong = false, children }: { strong?: boolean; children: ReactNode }) {
  return <span className={cn('whitespace-nowrap tabular-nums', strong && 'font-bold')}>{children}</span>
}
