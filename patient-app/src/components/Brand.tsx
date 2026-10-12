import { HeartPulse } from 'lucide-react'
import { cn } from '@atheris/ui'

/** The product mark and name. */
export function Brand({ className }: { className?: string }) {
  return (
    <div className={cn('flex items-center gap-2.5', className)}>
      <span className="bg-secondary text-on-secondary flex size-9 items-center justify-center rounded-xl">
        <HeartPulse className="size-5" aria-hidden />
      </span>
      <span className="font-display text-primary text-lg font-bold tracking-tight">Atheris Health</span>
    </div>
  )
}
