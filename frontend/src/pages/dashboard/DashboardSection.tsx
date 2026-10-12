import { useId, type ReactNode } from 'react'
import { RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import type { DashboardSectionName } from './dashboardSections'

interface DashboardSectionProps {
  /** The heading, which also names the region. */
  title: string
  /** Which dashboard this is, as the error message says it. */
  name: DashboardSectionName
  /** The dashboard could not be read and there is nothing to show in its place. */
  isError: boolean
  /**
   * The figures on show were read earlier and a later read failed (after the
   * user's own write, say), so they may be out of date.
   */
  isStale: boolean
  onRetry: () => void
  children: ReactNode
}

/**
 * The frame of one role section: a headed region and, when its dashboard
 * could not be read, one alert with Retry in place of the tiles. When a later
 * read fails the figures already on show stay, under a line saying they could
 * not be refreshed. The wording is fixed — nothing the server said is shown —
 * and claims no cause: a refusal (an account with no hospital) lands here too.
 */
export function DashboardSection({
  title,
  name,
  isError,
  isStale,
  onRetry,
  children,
}: DashboardSectionProps) {
  const headingId = useId()
  return (
    <section aria-labelledby={headingId} className="min-w-0 space-y-4">
      <h2 id={headingId} className="font-display text-title-lg text-primary font-bold">
        {title}
      </h2>
      {isError ? (
        <Alert variant="error" title={`Couldn't load the ${name} dashboard`}>
          <p>These figures are not available.</p>
          <Button variant="outline" size="sm" className="mt-3" onClick={onRetry}>
            <RotateCw aria-hidden /> Retry
          </Button>
        </Alert>
      ) : (
        <>
          {isStale && (
            <div
              role="status"
              className="font-body text-body-sm text-on-surface-variant flex flex-wrap items-center gap-x-3 gap-y-1"
            >
              <span className="min-w-0 break-words">
                Couldn't refresh these figures. They may be out of date.
              </span>
              <Button variant="outline" size="sm" onClick={onRetry}>
                <RotateCw aria-hidden /> Retry
              </Button>
            </div>
          )}
          {children}
        </>
      )}
    </section>
  )
}
