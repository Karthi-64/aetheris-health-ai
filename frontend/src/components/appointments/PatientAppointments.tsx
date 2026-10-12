import { useState } from 'react'
import { CalendarPlus, FlaskConical, Pill } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useAppointments, type AppointmentSummary } from '@/api/appointments'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDate, formatTime } from '@/lib/format'
import { AppointmentStatusBadge } from './AppointmentStatusBadge'
import { OrderLabTestsDialog } from '@/components/laboratory/OrderLabTestsDialog'
import { canOrderLabTestsFor } from '@/components/laboratory/labPresentation'
import { PrescribeDialog } from '@/components/pharmacy/PrescribeDialog'
import { canPrescribeFor } from '@/components/pharmacy/pharmacyPresentation'
import { BookAppointmentDialog } from './BookAppointmentDialog'

/** How many appointments to show before pointing at the rest. */
const SHOWN = 5

/** The API's page-size ceiling. One page is a patient's whole history in practice. */
const PAGE_SIZE = 100

const OPEN: ReadonlySet<AppointmentSummary['status']> = new Set(['booked', 'checked_in', 'in_progress'])

/**
 * What to show first for a patient: the visits still ahead, soonest first,
 * then the most recent ones behind. The API returns them oldest first and has
 * no "from today" filter, so the ordering is done here.
 */
function mostRelevant(appointments: AppointmentSummary[], now: number): AppointmentSummary[] {
  const ahead = appointments.filter(
    (a) => OPEN.has(a.status) && new Date(a.scheduled_end).getTime() >= now,
  )
  const behind = appointments.filter((a) => !ahead.includes(a)).reverse()
  return [...ahead, ...behind].slice(0, SHOWN)
}

/**
 * A patient's appointments on their record, with booking one from there —
 * the front desk's path from "found the patient" to "booked the visit".
 * Renders nothing for a user who cannot read appointments.
 */
export function PatientAppointments({
  patient,
}: {
  patient: { id: string; full_name: string; mrn: string; status: 'active' | 'inactive' }
}) {
  const { can } = usePermissions()
  const allowed = can('appointment.read')
  const { data, isPending, isError } = useAppointments(
    { patient_id: patient.id, page: 1, page_size: PAGE_SIZE },
    { enabled: allowed },
  )
  // Read once: "ahead" should not reshuffle on every render.
  const [now] = useState(() => Date.now())
  if (!allowed) return null

  const total = data?.pagination.total ?? 0
  const shown = mostRelevant(data?.items ?? [], now)
  // The API refuses to book for a deactivated patient.
  const canBook = can('appointment.book') && patient.status === 'active'
  const canOrderTests = can('lab.order.create') && can('lab.test.read')
  const canPrescribe = can('pharmacy.prescription.create') && can('pharmacy.medicine.read')

  return (
    <section className="neo-extruded bg-surface rounded-2xl p-6" aria-label="Appointments">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <h2 className="font-display text-title-lg text-primary font-bold">Appointments</h2>
        {canBook && (
          <BookAppointmentDialog
            patient={patient}
            trigger={
              <Button variant="outline" size="sm">
                <CalendarPlus className="size-4" /> Book appointment
              </Button>
            }
          />
        )}
      </div>

      {isError ? (
        <p className="font-body text-body-sm text-error">This patient's appointments couldn't be loaded.</p>
      ) : isPending ? (
        <div role="status" aria-label="Loading appointments" className="space-y-2">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-10 w-full rounded-xl" />
        </div>
      ) : shown.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">
          No appointments for this patient yet.
        </p>
      ) : (
        <>
          <ul className="divide-outline-variant/20 divide-y">
            {shown.map((a) => (
              <li key={a.id} className="flex flex-wrap items-center justify-between gap-3 px-2 py-2.5">
                <span className="font-body text-body-sm text-on-surface">
                  <span className="font-semibold tabular-nums">
                    {formatDate(a.scheduled_start)}, {formatTime(a.scheduled_start)}
                  </span>
                  <span className="text-outline"> · </span>
                  {a.doctor_name}
                </span>
                <span className="flex flex-wrap items-center gap-2">
                  {canOrderTests && canOrderLabTestsFor(a.status) && (
                    <OrderLabTestsDialog
                      visit={a}
                      trigger={
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Order lab tests for the visit on ${formatDate(a.scheduled_start)}`}
                        >
                          <FlaskConical className="size-4" /> Lab tests
                        </Button>
                      }
                    />
                  )}
                  {canPrescribe && canPrescribeFor(a.status) && (
                    <PrescribeDialog
                      visit={a}
                      trigger={
                        <Button
                          variant="ghost"
                          size="sm"
                          aria-label={`Write prescription for the visit on ${formatDate(a.scheduled_start)}`}
                        >
                          <Pill className="size-4" /> Prescribe
                        </Button>
                      }
                    />
                  )}
                  <AppointmentStatusBadge status={a.status} />
                </span>
              </li>
            ))}
          </ul>
          {total > shown.length && (
            <p className="font-body text-outline mt-3 text-xs">
              Showing {shown.length} of {total} appointments.
            </p>
          )}
        </>
      )}
    </section>
  )
}
