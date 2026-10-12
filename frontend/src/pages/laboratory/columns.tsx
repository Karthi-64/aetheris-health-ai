import type { ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import type { LabOrder } from '@/api/lab'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime } from '@/lib/format'
import { LabAbnormalMarker, LabOrderStatusBadge, LabPriorityBadge } from '@/components/laboratory/LabBadges'

/** Patient name, linked to the record for users who may open it. */
function PatientCell({ order }: { order: LabOrder }) {
  const { can } = usePermissions()
  return (
    <div className="flex min-w-36 flex-col">
      {can('patient.read') ? (
        <Link
          to={`/patients/${order.patient_id}`}
          className="text-on-surface hover:text-secondary font-semibold hover:underline"
        >
          {order.patient_name}
        </Link>
      ) : (
        <span className="text-on-surface font-semibold">{order.patient_name}</span>
      )}
      <span className="text-outline font-mono text-xs">{order.patient_mrn}</span>
    </div>
  )
}

/** Worklist columns. Sorting is off: the API returns newest first and has no sort parameter. */
export const labOrderColumns: ColumnDef<LabOrder>[] = [
  {
    accessorKey: 'ordered_at',
    header: 'Ordered',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant whitespace-nowrap tabular-nums">
        {formatDateTime(row.original.ordered_at)}
      </span>
    ),
  },
  {
    id: 'patient',
    header: 'Patient',
    enableSorting: false,
    cell: ({ row }) => <PatientCell order={row.original} />,
  },
  {
    accessorKey: 'doctor_name',
    header: 'Ordered by',
    enableSorting: false,
    cell: ({ row }) => <span className="text-on-surface-variant">{row.original.doctor_name}</span>,
  },
  {
    id: 'tests',
    header: 'Tests',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface" title={row.original.items.map((i) => i.test_name).join(', ')}>
        {row.original.items.map((i) => i.test_code).join(', ')}
      </span>
    ),
  },
  {
    accessorKey: 'priority',
    header: 'Priority',
    enableSorting: false,
    cell: ({ row }) => <LabPriorityBadge priority={row.original.priority} />,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex flex-wrap items-center gap-1.5">
        <LabOrderStatusBadge status={row.original.status} />
        <LabAbnormalMarker order={row.original} />
      </div>
    ),
  },
  {
    id: 'open',
    header: '',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <Link
          to={`/laboratory/orders/${row.original.id}`}
          aria-label={`Open lab order for ${row.original.patient_name}, ordered ${formatDateTime(row.original.ordered_at)}`}
          className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
        >
          Open <ChevronRight className="size-4" />
        </Link>
      </div>
    ),
  },
]
