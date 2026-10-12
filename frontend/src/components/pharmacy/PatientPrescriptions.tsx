import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { usePrescriptions } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime } from '@/lib/format'
import { PrescriptionStatusBadge } from './PharmacyBadges'

/** How many of the newest prescriptions to show before linking to the full list. */
const RECENT = 5

/**
 * A patient's most recent prescriptions, linking into Pharmacy. Renders
 * nothing, and asks the API nothing, for a user who cannot read prescriptions.
 */
export function PatientPrescriptions({ patientId }: { patientId: string }) {
  const { can } = usePermissions()
  const allowed = can('pharmacy.prescription.read')
  const { data, isPending, isError } = usePrescriptions(
    { patient_id: patientId, page_size: RECENT },
    { enabled: allowed },
  )
  if (!allowed) return null

  const prescriptions = data?.items ?? []
  const total = data?.pagination.total ?? 0

  return (
    <section className="neo-extruded bg-surface rounded-2xl p-6" aria-label="Prescriptions">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="font-display text-title-lg text-primary font-bold">Prescriptions</h2>
        {total > 0 && (
          <Link
            to={`/pharmacy/prescriptions?patient_id=${patientId}`}
            className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
          >
            {total > RECENT ? `View all ${total} prescriptions` : 'Open in Pharmacy'}
            <ChevronRight className="size-4" />
          </Link>
        )}
      </div>

      {isError ? (
        <p className="font-body text-body-sm text-error">
          This patient's prescriptions couldn't be loaded.
        </p>
      ) : isPending ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading prescriptions">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-10 w-full rounded-xl" />
        </div>
      ) : prescriptions.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">
          No prescriptions for this patient yet.
        </p>
      ) : (
        <ul className="divide-outline-variant/20 divide-y">
          {prescriptions.map((prescription) => (
            <li key={prescription.id}>
              <Link
                to={`/pharmacy/prescriptions/${prescription.id}`}
                className="hover:bg-surface-container-low/60 flex flex-wrap items-center justify-between gap-3 rounded-lg px-2 py-2.5 transition-colors"
              >
                <span className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="font-body text-body-sm text-on-surface font-semibold [overflow-wrap:anywhere]">
                    {prescription.items.map((i) => i.medicine_name).join(', ')}
                  </span>
                  <span className="font-body text-outline text-xs tabular-nums">
                    {formatDateTime(prescription.prescribed_at)}
                  </span>
                </span>
                <PrescriptionStatusBadge status={prescription.status} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
