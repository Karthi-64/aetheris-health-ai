import { Badge } from '@/components/ui/badge'
import type { AppointmentStatus } from '@/api/appointments'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

const STATUS: Record<AppointmentStatus, { label: string; variant: Variant }> = {
  booked: { label: 'Booked', variant: 'neutral' },
  checked_in: { label: 'Checked in', variant: 'accent' },
  in_progress: { label: 'In progress', variant: 'warning' },
  completed: { label: 'Completed', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
  no_show: { label: 'No show', variant: 'critical' },
}

export function AppointmentStatusBadge({ status }: { status: AppointmentStatus }) {
  const s = STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}
