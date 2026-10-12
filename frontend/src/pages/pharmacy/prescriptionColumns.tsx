import type { ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import type { Prescription } from '@/api/pharmacy'
import { formatDateTime } from '@/lib/format'
import { PrescriptionStatusBadge } from '@/components/pharmacy/PharmacyBadges'
import { PrescriptionAvailabilityCell, PrescriptionPatientCell } from './PrescriptionsPageCells'

/** How many medicine names a row shows before the rest are counted. */
const NAMES_SHOWN = 2

/**
 * Prescription list columns, for both the dispensing queue and the full list.
 * Sorting is off: the API orders each (longest waiting first; newest first)
 * and has no sort parameter.
 */
export const prescriptionColumns: ColumnDef<Prescription>[] = [
  {
    accessorKey: 'prescribed_at',
    header: 'Prescribed',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant whitespace-nowrap tabular-nums">
        {formatDateTime(row.original.prescribed_at)}
      </span>
    ),
  },
  {
    id: 'patient',
    header: 'Patient',
    enableSorting: false,
    cell: ({ row }) => <PrescriptionPatientCell prescription={row.original} />,
  },
  {
    accessorKey: 'doctor_name',
    header: 'Prescriber',
    enableSorting: false,
    // The API's name already carries "Dr.".
    cell: ({ row }) => <span className="text-on-surface-variant">{row.original.doctor_name}</span>,
  },
  {
    id: 'medicines',
    header: 'Medicines',
    enableSorting: false,
    cell: ({ row }) => {
      const names = row.original.items.map((i) => i.medicine_name)
      const more = names.length - NAMES_SHOWN
      return (
        <span className="text-on-surface block min-w-40" title={names.join(', ')}>
          {names.slice(0, NAMES_SHOWN).join(', ')}
          {more > 0 && <span className="text-outline"> +{more} more</span>}
        </span>
      )
    },
  },
  {
    accessorKey: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => <PrescriptionStatusBadge status={row.original.status} />,
  },
  {
    id: 'availability',
    header: 'Availability',
    enableSorting: false,
    cell: ({ row }) => <PrescriptionAvailabilityCell prescription={row.original} />,
  },
  {
    id: 'open',
    header: '',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <Link
          to={`/pharmacy/prescriptions/${row.original.id}`}
          aria-label={`Open prescription for ${row.original.patient_name}, prescribed ${formatDateTime(row.original.prescribed_at)}`}
          className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
        >
          Open <ChevronRight className="size-4" />
        </Link>
      </div>
    ),
  },
]
