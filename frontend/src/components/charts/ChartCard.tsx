import { useId, type ReactNode } from 'react'
import { BarChart3, RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'

interface ChartCardProps {
  title: string
  description?: string
  isLoading: boolean
  isError: boolean
  onRetry: () => void
  /** True when the request answered with nothing to draw (every figure zero). */
  isEmpty: boolean
  emptyTitle: string
  emptyDescription?: string
  /**
   * One sentence saying what the chart shows, with the server's own headline
   * figures. It is the chart's text alternative: read by a screen reader in
   * place of the drawing. The full figures belong in a table beside the card.
   */
  summary: string
  /** Controls shown beside the title. */
  actions?: ReactNode
  /** The chart itself. Only rendered when there is something to draw. */
  children: ReactNode
}

/**
 * The frame every report chart sits in. It owns the four states — loading
 * (a skeleton, never placeholder bars), failed (a message and Retry, never the
 * server's raw text), empty and drawn — so no chart is shown for data that has
 * not arrived.
 *
 * The drawing is hidden from assistive technology and `summary` is read in its
 * place; the page shows the same rows as a table for the detail.
 */
export function ChartCard({
  title,
  description,
  isLoading,
  isError,
  onRetry,
  isEmpty,
  emptyTitle,
  emptyDescription,
  summary,
  actions,
  children,
}: ChartCardProps) {
  const titleId = useId()

  let body: ReactNode
  if (isLoading) {
    body = (
      <div role="status" aria-label={`Loading ${title}`}>
        <Skeleton className="h-[280px] w-full rounded-xl" />
      </div>
    )
  } else if (isError) {
    body = (
      <Alert variant="error" title="Couldn't load this chart">
        <div className="flex flex-col items-start gap-3">
          <p>The figures could not be reached. Check your connection and try again.</p>
          <Button variant="outline" size="sm" onClick={onRetry}>
            <RotateCw className="size-4" aria-hidden /> Retry
          </Button>
        </div>
      </Alert>
    )
  } else if (isEmpty) {
    body = <EmptyState icon={BarChart3} title={emptyTitle} description={emptyDescription} className="py-10" />
  } else {
    body = (
      <figure className="m-0 min-w-0">
        <figcaption className="sr-only">{summary}</figcaption>
        <div aria-hidden className="min-w-0 overflow-hidden">
          {children}
        </div>
      </figure>
    )
  }

  return (
    <section aria-labelledby={titleId} className="neo-extruded bg-surface min-w-0 rounded-2xl p-5">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id={titleId} className="font-display text-title-lg text-primary font-bold">
            {title}
          </h2>
          {description && (
            <p className="font-body text-body-sm text-on-surface-variant mt-1">{description}</p>
          )}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {body}
    </section>
  )
}
