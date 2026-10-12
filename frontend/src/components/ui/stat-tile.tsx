import { RotateCw, type LucideIcon } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { KpiCard } from '@/components/ui/kpi-card'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

interface StatTileProps {
  label: string
  /** The figure as the server sent it (formatted, never computed). Left out while it is unknown. */
  value?: string | number
  /** A second line under the value. */
  hint?: string
  icon?: LucideIcon
  /** Where the figure is broken down. Makes the tile a link once it has a value. */
  to?: string
  isLoading: boolean
  isError: boolean
  /** Shown as a Retry button when the figure could not be read. */
  onRetry?: () => void
  className?: string
}

/**
 * One server-backed figure. It never shows a number it does not have: a
 * skeleton while loading, and "—" with a Retry button when the request failed
 * or answered without the value.
 */
export function StatTile({
  label,
  value,
  hint,
  icon: Icon,
  to,
  isLoading,
  isError,
  onRetry,
  className,
}: StatTileProps) {
  if (isLoading) {
    return (
      <div role="status" aria-label={`Loading ${label}`} className={cn('min-w-0', className)}>
        <Skeleton className="h-[104px] rounded-2xl" />
      </div>
    )
  }

  if (isError || value === undefined) {
    return (
      <div className={cn('neo-extruded bg-surface min-w-0 rounded-2xl p-5', className)}>
        <div className="flex items-center justify-between gap-3">
          <p className="font-label text-label-caps text-on-surface-variant min-w-0">{label}</p>
          {Icon && (
            <span className="bg-secondary/10 text-secondary flex size-9 items-center justify-center rounded-xl">
              <Icon className="size-5" aria-hidden />
            </span>
          )}
        </div>
        <p className="font-display text-headline-md text-primary mt-3 font-bold">
          <span aria-hidden>—</span>
          <span className="sr-only">Not available</span>
        </p>
        {isError && (
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <p className="font-body text-body-sm text-on-surface-variant">Couldn't load this figure.</p>
            {onRetry && (
              <Button variant="outline" size="xs" onClick={onRetry} aria-label={`Retry ${label}`}>
                <RotateCw aria-hidden /> Retry
              </Button>
            )}
          </div>
        )}
      </div>
    )
  }

  return <KpiCard label={label} value={value} hint={hint} icon={Icon} to={to} className={className} />
}
