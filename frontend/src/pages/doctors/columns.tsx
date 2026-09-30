import type { ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { formatMoney } from '@/lib/format'
import type { DoctorStatus, DoctorSummary } from '@/api/doctors'

const STATUS_VARIANT: Record<DoctorStatus, 'success' | 'neutral'> = {
  active: 'success',
  inactive: 'neutral',
}

export const doctorColumns: ColumnDef<DoctorSummary>[] = [
  {
    accessorKey: 'full_name',
    header: 'Name',
    cell: ({ row }) => <span className="font-semibold">{row.original.full_name}</span>,
  },
  { accessorKey: 'specialization', header: 'Specialization' },
  {
    accessorKey: 'department_name',
    header: 'Department',
    cell: ({ row }) =>
      row.original.department_name ?? <span className="text-outline-variant">Unassigned</span>,
  },
  {
    accessorKey: 'consultation_fee',
    header: 'Fee',
    cell: ({ row }) => <span className="font-mono">{formatMoney(row.original.consultation_fee)}</span>,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => (
      <Badge variant={STATUS_VARIANT[row.original.status]} className="capitalize">
        {row.original.status}
      </Badge>
    ),
  },
  {
    id: 'actions',
    header: '',
    cell: ({ row }) => (
      <Link
        to={`/doctors/${row.original.id}`}
        aria-label={`View ${row.original.full_name}`}
        className="text-outline hover:text-secondary inline-flex items-center gap-1 transition-colors"
      >
        View <ChevronRight className="size-4" />
      </Link>
    ),
  },
]
