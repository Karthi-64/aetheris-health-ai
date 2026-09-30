import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/** A labelled value inside an {@link InfoCard}. Renders an em-dash for empty values. */
export function Detail({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="space-y-0.5">
      <p className="font-label text-label-caps text-on-surface-variant">{label}</p>
      <div className="font-body text-body-md text-on-surface">
        {value === null || value === undefined || value === '' ? (
          <span className="text-outline-variant">—</span>
        ) : (
          value
        )}
      </div>
    </div>
  )
}

/** Clinical Glass card grouping a set of {@link Detail} rows under a title. */
export function InfoCard({
  title,
  children,
  className,
}: {
  title: string
  children: ReactNode
  className?: string
}) {
  return (
    <section className={cn('neo-extruded bg-surface rounded-2xl p-6', className)}>
      <h2 className="font-display text-title-lg text-primary mb-4 font-bold">{title}</h2>
      <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-3">{children}</div>
    </section>
  )
}
