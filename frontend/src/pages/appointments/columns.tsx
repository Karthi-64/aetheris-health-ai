import type { ColumnDef } from '@tanstack/react-table'
import { Badge } from '@/components/ui/badge'
import { formatTime } from '@/lib/format'
import type { AppointmentStatus, AppointmentSummary, AppointmentType } from '@/api/appointments'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

const STATUS: Record<AppointmentStatus, { label: string; variant: Variant }> = {
  booked: { label: 'Booked', variant: 'neutral' },
  checked_in: { label: 'Checked in', variant: 'accent' },
  in_progress: { label: 'In progress', variant: 'warning' },
  completed: { label: 'Completed', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
  no_show: { label: 'No show', variant: 'critical' },
}

const TYPE: Record<AppointmentType, { label: string; variant: Variant }> = {
  new: { label: 'New', variant: 'primary' },
  follow_up: { label: 'Follow-up', variant: 'accent' },
  walk_in: { label: 'Walk-in', variant: 'neutral' },
  emergency: { label: 'Emergency', variant: 'critical' },
}

export function AppointmentStatusBadge({ status }: { status: AppointmentStatus }) {
  const s = STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

export const appointmentColumns: ColumnDef<AppointmentSummary>[] = [
  {
    accessorKey: 'scheduled_start',
    header: 'Time',
    cell: ({ row }) => <span className="font-mono">{formatTime(row.original.scheduled_start)}</span>,
  },
  {
    accessorKey: 'patient_name',
    header: 'Patient',
    cell: ({ row }) => <span className="font-semibold">{row.original.patient_name}</span>,
  },
  { accessorKey: 'doctor_name', header: 'Doctor' },
  {
    accessorKey: 'type',
    header: 'Type',
    cell: ({ row }) => {
      const t = TYPE[row.original.type]
      return <Badge variant={t.variant}>{t.label}</Badge>
    },
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => <AppointmentStatusBadge status={row.original.status} />,
  },
]
