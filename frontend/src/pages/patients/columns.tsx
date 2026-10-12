import type { ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { GENDER_LABELS, type PatientStatus, type PatientSummary } from '@/api/patients'

/**
 * Columns for the patient registry.
 *
 * Doctor, department and a clinical status (Admitted / Critical / …) used to be
 * shown here. None of them exist on a patient record: the treating doctor lives
 * on appointments, and there is no admissions module, so those columns were
 * displaying values the mock invented. `status` below is the record's
 * active/inactive lifecycle (`docs/18-API_CONTRACTS.md` §2.2).
 */
const STATUS_VARIANT: Record<PatientStatus, 'accent' | 'neutral'> = {
  active: 'accent',
  inactive: 'neutral',
}

const STATUS_LABEL: Record<PatientStatus, string> = {
  active: 'Active',
  inactive: 'Inactive',
}

export const patientColumns: ColumnDef<PatientSummary>[] = [
  {
    accessorKey: 'mrn',
    header: 'MRN',
    cell: ({ row }) => <span className="font-mono text-primary">{row.original.mrn}</span>,
  },
  {
    accessorKey: 'full_name',
    header: 'Name',
    cell: ({ row }) => <span className="font-semibold">{row.original.full_name}</span>,
  },
  {
    id: 'ageGender',
    header: 'Age / Gender',
    accessorFn: (p) => `${p.age} · ${GENDER_LABELS[p.gender]}`,
    enableSorting: false,
  },
  {
    accessorKey: 'date_of_birth',
    header: 'Date of birth',
    cell: ({ row }) => (
      <span className="tabular-nums">
        {new Date(row.original.date_of_birth).toLocaleDateString()}
      </span>
    ),
  },
  {
    accessorKey: 'phone',
    header: 'Phone',
    cell: ({ row }) => row.original.phone ?? <span className="text-outline">—</span>,
  },
  {
    accessorKey: 'status',
    header: 'Status',
    cell: ({ row }) => (
      <Badge variant={STATUS_VARIANT[row.original.status]}>
        {STATUS_LABEL[row.original.status]}
      </Badge>
    ),
  },
  {
    id: 'actions',
    header: '',
    cell: ({ row }) => (
      <Link
        to={`/patients/${row.original.id}`}
        // Two patients can share a name; the MRN is what tells their links apart.
        aria-label={`View ${row.original.full_name}, ${row.original.mrn}`}
        className="text-outline hover:text-secondary inline-flex items-center gap-1 transition-colors"
      >
        View <ChevronRight className="size-4" />
      </Link>
    ),
  },
]
