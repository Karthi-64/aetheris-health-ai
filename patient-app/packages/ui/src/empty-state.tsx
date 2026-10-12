import type { ReactNode } from 'react'
import type { LucideIcon } from 'lucide-react'
import { cn } from './lib/utils'

interface EmptyStateProps {
  icon?: LucideIcon
  title: string
  description?: string
  /** Primary action (e.g. a "Register patient" button). */
  action?: ReactNode
  className?: string
}

/** Meaningful empty state with an optional CTA (cross-cutting standard, Part 2C). */
export function EmptyState({ icon: Icon, title, description, action, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center justify-center gap-4 rounded-2xl px-6 py-16 text-center',
        className,
      )}
    >
      {Icon && (
        <span className="neo-pressed bg-surface text-outline flex size-14 items-center justify-center rounded-2xl">
          <Icon className="size-7" aria-hidden />
        </span>
      )}
      <div className="space-y-1">
        <p className="font-display text-title-lg text-primary font-bold">{title}</p>
        {description && (
          <p className="font-body text-body-sm text-on-surface-variant mx-auto max-w-sm">
            {description}
          </p>
        )}
      </div>
      {action}
    </div>
  )
}
