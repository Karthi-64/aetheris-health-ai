import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, MinusCircle } from 'lucide-react'
import type { Prescription } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { availabilityOf } from './dispenseForm'

/*
 * The cells of the prescription list that are components. They live apart
 * from `prescriptionColumns.tsx` because a file that exports the column list
 * cannot also hold components without breaking Fast Refresh.
 */

/** Patient name, linked to the record for users who may open it. A pharmacist may not. */
export function PrescriptionPatientCell({ prescription }: { prescription: Prescription }) {
  const { can } = usePermissions()
  return (
    <div className="flex min-w-36 flex-col">
      {can('patient.read') ? (
        <Link
          to={`/patients/${prescription.patient_id}`}
          className="text-on-surface hover:text-secondary font-semibold hover:underline"
        >
          {prescription.patient_name}
        </Link>
      ) : (
        <span className="text-on-surface font-semibold">{prescription.patient_name}</span>
      )}
      <span className="text-outline font-mono text-xs">{prescription.patient_mrn}</span>
    </div>
  )
}

/**
 * Whether a waiting prescription can be dispensed in full today, in words —
 * read off the numbers the API sent with each line (see {@link availabilityOf}).
 * Blank once a prescription is dispensed or cancelled.
 */
export function PrescriptionAvailabilityCell({ prescription }: { prescription: Prescription }) {
  const availability = availabilityOf(prescription)
  if (!availability) return <span className="text-outline-variant">—</span>
  if (availability.state === 'in_stock') {
    return (
      <span className="text-stable inline-flex items-center gap-1.5 whitespace-nowrap">
        <CheckCircle2 className="size-4 shrink-0" aria-hidden /> In stock
      </span>
    )
  }
  if (availability.state === 'nothing') {
    return (
      <span className="text-on-surface-variant inline-flex items-center gap-1.5">
        <MinusCircle className="size-4 shrink-0" aria-hidden /> Nothing to dispense here
      </span>
    )
  }
  return (
    <span className="inline-flex min-w-40 items-start gap-1.5 text-amber-600">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>Short: {availability.medicines.join(', ')}</span>
    </span>
  )
}
