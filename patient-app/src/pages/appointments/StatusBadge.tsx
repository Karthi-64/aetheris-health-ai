import { cn } from '@atheris/ui'
import type { AppointmentStatus } from '@/api/myAppointments'
import { statusLabels } from '@/pages/appointments/strings'

const TONE: Record<AppointmentStatus, string> = {
  booked: 'bg-secondary-fixed/60 text-on-secondary-container',
  checked_in: 'bg-secondary-fixed/60 text-on-secondary-container',
  in_progress: 'bg-secondary-fixed/60 text-on-secondary-container',
  completed: 'bg-surface-container text-on-surface-variant',
  cancelled: 'bg-error-container text-on-error-container',
  no_show: 'bg-error-container text-on-error-container',
}

/** An appointment's state, in the app's own word for it. The state is the server's; only the word is ours. */
export function StatusBadge({ status, className }: { status: AppointmentStatus; className?: string }) {
  return (
    <span className={cn('text-body-sm inline-block rounded-full px-2.5 py-0.5 font-medium', TONE[status], className)}>
      {statusLabels[status]}
    </span>
  )
}
