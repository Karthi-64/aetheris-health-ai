import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { Badge } from '@/components/ui/badge'
import { formatTime } from '@/lib/format'
import type { AppointmentSummary, AppointmentType } from '@/api/appointments'
import { AppointmentStatusBadge } from '@/components/appointments/AppointmentStatusBadge'
import { usePermissions } from '@/hooks/usePermissions'
import { AppointmentActions } from './AppointmentActions'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

const TYPE: Record<AppointmentType, { label: string; variant: Variant }> = {
  new: { label: 'New', variant: 'primary' },
  follow_up: { label: 'Follow-up', variant: 'accent' },
  walk_in: { label: 'Walk-in', variant: 'neutral' },
  emergency: { label: 'Emergency', variant: 'critical' },
}

/** The patient's name, linking to their record for a user who may open it. */
function PatientCell({ appointment }: { appointment: AppointmentSummary }) {
  const { can } = usePermissions()
  if (!can('patient.read')) return <span className="font-semibold">{appointment.patient_name}</span>
  return (
    <Link
      to={`/patients/${appointment.patient_id}`}
      className="hover:text-secondary font-semibold transition-colors hover:underline"
    >
      {appointment.patient_name}
    </Link>
  )
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
    cell: ({ row }) => <PatientCell appointment={row.original} />,
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

/**
 * The queue's columns: the shared ones plus the lifecycle actions. The
 * dashboard's table stays read-only and uses `appointmentColumns` alone.
 */
export const appointmentQueueColumns: ColumnDef<AppointmentSummary>[] = [
  ...appointmentColumns,
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    cell: ({ row }) => <AppointmentActions appointment={row.original} />,
  },
]
