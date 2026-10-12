import type { ComponentProps } from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { AlertTriangle, CheckCircle2, Info, XCircle, type LucideIcon } from 'lucide-react'
import { cn } from './lib/utils'

const alertVariants = cva('flex items-start gap-3 rounded-xl border p-4', {
  variants: {
    variant: {
      info: 'border-secondary/30 bg-secondary/5 text-on-surface',
      success: 'border-stable/30 bg-stable/5 text-on-surface',
      warning: 'border-amber-500/30 bg-amber-500/5 text-on-surface',
      error: 'border-error/30 bg-error/5 text-on-surface',
    },
  },
  defaultVariants: { variant: 'info' },
})

const ICONS: Record<NonNullable<VariantProps<typeof alertVariants>['variant']>, LucideIcon> = {
  info: Info,
  success: CheckCircle2,
  warning: AlertTriangle,
  error: XCircle,
}

const ICON_TONE = {
  info: 'text-secondary',
  success: 'text-stable',
  warning: 'text-amber-600',
  error: 'text-error',
} as const

interface AlertProps extends ComponentProps<'div'>, VariantProps<typeof alertVariants> {
  title?: string
}

function Alert({ className, variant = 'info', title, children, ...props }: AlertProps) {
  const v = variant ?? 'info'
  const Icon = ICONS[v]
  return (
    <div role="alert" className={cn(alertVariants({ variant }), className)} {...props}>
      <Icon className={cn('mt-0.5 size-5 shrink-0', ICON_TONE[v])} aria-hidden />
      <div className="min-w-0 flex-1">
        {title && <p className="font-body text-body-md font-bold">{title}</p>}
        {children && (
          <div className="font-body text-body-sm text-on-surface-variant">{children}</div>
        )}
      </div>
    </div>
  )
}

export { Alert, alertVariants }
