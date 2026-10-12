import type { ComponentProps } from 'react'
import { cn } from './lib/utils'

/** Loading placeholder — used above the fold instead of spinners (CLAUDE.md). */
function Skeleton({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      data-slot="skeleton"
      aria-hidden
      className={cn('bg-surface-container-high animate-pulse rounded-lg', className)}
      {...props}
    />
  )
}

export { Skeleton }
