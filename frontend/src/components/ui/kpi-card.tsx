import { Link } from 'react-router-dom'
import { ArrowDownRight, ArrowUpRight, type LucideIcon } from 'lucide-react'
import { cn } from '@/lib/utils'

interface KpiCardProps {
  label: string
  value: string | number
  icon?: LucideIcon
  /** Signed percentage change vs the prior period, e.g. +12.5 or -3. */
  deltaPct?: number
  /** For a delta where "down is good" (e.g. wait times), flip the color mapping. */
  invertDelta?: boolean
  /** A second line under the value, e.g. "Collected ₹2,800.00". */
  hint?: string
  /** Where the figure is broken down. Makes the whole tile a link. */
  to?: string
  className?: string
}

/** Compact metric tile for dashboards and report headers (spec Part 11). */
export function KpiCard({
  label,
  value,
  icon: Icon,
  deltaPct,
  invertDelta,
  hint,
  to,
  className,
}: KpiCardProps) {
  const hasDelta = typeof deltaPct === 'number'
  const positive = hasDelta && deltaPct >= 0
  const good = invertDelta ? !positive : positive

  const body = (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="font-label text-label-caps text-on-surface-variant min-w-0">{label}</p>
        {Icon && (
          <span className="bg-secondary/10 text-secondary flex size-9 items-center justify-center rounded-xl">
            <Icon className="size-5" aria-hidden />
          </span>
        )}
      </div>
      <p className="font-display text-headline-md text-primary mt-3 font-bold break-words">{value}</p>
      {hint && <p className="font-body text-body-sm text-on-surface-variant mt-1 break-words">{hint}</p>}
      {hasDelta && (
        <p
          className={cn(
            'font-body text-body-sm mt-1 flex items-center gap-1',
            good ? 'text-stable' : 'text-critical',
          )}
        >
          {positive ? (
            <ArrowUpRight className="size-4" aria-hidden />
          ) : (
            <ArrowDownRight className="size-4" aria-hidden />
          )}
          {Math.abs(deltaPct)}%
          <span className="text-outline font-normal">vs last period</span>
        </p>
      )}
    </>
  )

  const surface = cn('neo-extruded bg-surface min-w-0 rounded-2xl p-5', className)
  if (to) {
    return (
      <Link
        to={to}
        className={cn(
          surface,
          'block transition-shadow outline-none hover:shadow-md focus-visible:ring-2 focus-visible:ring-secondary',
        )}
      >
        {body}
      </Link>
    )
  }
  return <div className={surface}>{body}</div>
}
