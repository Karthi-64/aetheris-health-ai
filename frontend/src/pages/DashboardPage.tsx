import { Link } from 'react-router-dom'
import { ArrowRight, CalendarClock, CalendarPlus, Stethoscope, UserPlus, Users } from 'lucide-react'
import { StatTile } from '@/components/ui/stat-tile'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { useAuthStore } from '@/store/auth-store'
import { usePermissions } from '@/hooks/usePermissions'
import { usePatients } from '@/api/patients'
import { useDoctors } from '@/api/doctors'
import { useAppointments } from '@/api/appointments'
import { RegisterPatientDialog } from '@/pages/patients/RegisterPatientDialog'
import { BookAppointmentDialog } from '@/components/appointments/BookAppointmentDialog'
import { InvoicesAwaitingPayment } from '@/components/billing/InvoicesAwaitingPayment'
import { appointmentQueueColumns } from '@/pages/appointments/columns'
import { AdminDashboardSection } from '@/pages/dashboard/AdminDashboardSection'
import { BillingDashboardSection } from '@/pages/dashboard/BillingDashboardSection'
import { DoctorDashboardSection } from '@/pages/dashboard/DoctorDashboardSection'
import { ReceptionDashboardSection } from '@/pages/dashboard/ReceptionDashboardSection'
import { dashboardSectionsFor, TILE_GRID } from '@/pages/dashboard/dashboardSections'
import { useHospitalToday } from '@/pages/dashboard/useHospitalToday'

/** The greeting for the viewer's own time of day. */
function greeting(hour = new Date().getHours()): string {
  if (hour < 12) return 'Good morning'
  if (hour < 17) return 'Good afternoon'
  return 'Good evening'
}

/**
 * What the greeting says this user can do here — only things their own
 * dashboard offers, so a lab technician is not told to book appointments.
 */
function summary(tasks: string[]): string {
  if (tasks.length === 0) return 'Your home page — open a module from the menu to start work.'
  const list = new Intl.ListFormat('en', { style: 'long', type: 'conjunction' }).format(tasks)
  return `Your operations hub — ${list}.`
}

export default function DashboardPage() {
  const name = useAuthStore((s) => s.user?.name) ?? 'there'
  const { can } = usePermissions()
  const canBook = can('appointment.book')
  const canRegister = can('patient.create')
  // The dashboard is every role's home page, but not every role may read
  // every module: a Lab Technician holds none of these and Billing Staff has
  // no appointment access. Each query runs only when the user may read it, and
  // its tile or section is simply not shown otherwise — asking anyway produced
  // 403s and a permanent "couldn't load" alert on the home page.
  const canSeePatients = can('patient.read')
  const canSeeDoctors = can('doctor.read')
  const canSeeAppointments = can('appointment.read')
  // The same rule InvoicesAwaitingPayment applies before it shows anything.
  const canTakePayments =
    can('invoice.read') && (can('invoice.payment.record') || can('invoice.payment.record.cash'))
  // A holder of a `report.*.read` code gets role sections with the server's
  // own figures. Everyone else (a nurse, say) keeps the three list counts.
  const showListTiles = !dashboardSectionsFor(can).any
  // The hospital's day, not the browser's: the queue must show the same day
  // the tiles above it count.
  const { today, isResolved } = useHospitalToday()

  const patients = usePatients(
    { page: 1, page_size: 1 },
    { enabled: showListTiles && canSeePatients },
  )
  const doctors = useDoctors({ page: 1, page_size: 1 }, { enabled: showListTiles && canSeeDoctors })
  const appts = useAppointments(
    { appointment_date: today, page: 1, page_size: 25 },
    { enabled: canSeeAppointments && isResolved },
  )
  const tileCount = [canSeeAppointments, canSeePatients, canSeeDoctors].filter(Boolean).length

  const todaysAppointments = appts.data?.items ?? []

  return (
    <div className="w-full space-y-6">
      {/* Greeting + quick actions */}
      <div className="neo-extruded bg-surface flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6 md:p-8">
        <div>
          <h1 className="font-display text-primary text-2xl font-bold md:text-headline-lg">
            {greeting()}, {name}
          </h1>
          <p className="font-body text-body-sm text-on-surface-variant mt-1 max-w-xl">
            {summary([
              ...(canRegister ? ['register patients'] : []),
              ...(canBook ? ['book appointments'] : []),
              ...(canSeeAppointments ? ["follow the day's queue"] : []),
              ...(canTakePayments ? ['see invoices awaiting payment'] : []),
            ])}
          </p>
        </div>
        <div className="flex flex-wrap gap-3">
          {/* Hidden per permission (PR #29 review finding 10) — a Nurse holds
              neither patient.create nor appointment.book, so these controls
              used to open a dialog that could only end in a 403. */}
          {canRegister && (
            <RegisterPatientDialog
              trigger={
                <Button variant="outline" className="rounded-full">
                  <UserPlus className="size-4" /> Register Patient
                </Button>
              }
            />
          )}
          {canBook && (
            <BookAppointmentDialog
              trigger={
                <Button className="rounded-full">
                  <CalendarPlus className="size-4" /> Book Appointment
                </Button>
              }
            />
          )}
        </div>
      </div>

      {/* Role sections: each renders only for the permission that opens it,
          and asks only for its own dashboard. */}
      <AdminDashboardSection />
      <BillingDashboardSection />
      <ReceptionDashboardSection />
      <DoctorDashboardSection />

      {/* List counts for a user with no dashboard of their own — only the ones they may read */}
      {showListTiles && tileCount > 0 && (
        <div className={TILE_GRID[tileCount]}>
          {canSeeAppointments && (
            <StatTile
              label="Today's appointments"
              icon={CalendarClock}
              value={appts.data?.pagination.total}
              isLoading={appts.isPending}
              isError={appts.isError}
              onRetry={() => void appts.refetch()}
            />
          )}
          {canSeePatients && (
            <StatTile
              label="Total patients"
              icon={Users}
              value={patients.data?.pagination.total}
              isLoading={patients.isPending}
              isError={patients.isError}
              onRetry={() => void patients.refetch()}
            />
          )}
          {canSeeDoctors && (
            <StatTile
              label="Doctors"
              icon={Stethoscope}
              value={doctors.data?.pagination.total}
              isLoading={doctors.isPending}
              isError={doctors.isError}
              onRetry={() => void doctors.refetch()}
            />
          )}
        </div>
      )}

      {/* Today's queue, with the same actions as the Appointments page */}
      {canSeeAppointments && (
        <section className="space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="font-display text-title-lg text-primary font-bold">Today's appointments</h2>
            <Link
              to="/appointments"
              className="text-secondary font-body text-body-sm inline-flex items-center gap-1 hover:underline"
            >
              View all <ArrowRight className="size-4" />
            </Link>
          </div>

          {appts.isError ? (
            <Alert variant="error" title="Couldn't load today's appointments">
              The appointment queue could not be reached right now.
            </Alert>
          ) : (
            <DataTable
              columns={appointmentQueueColumns}
              data={todaysAppointments}
              isLoading={appts.isPending}
              pageSize={25}
              emptyState={
                <EmptyState
                  icon={CalendarClock}
                  title="No appointments today"
                  description="Booked appointments for today will appear here."
                  action={
                    canBook ? (
                      <BookAppointmentDialog
                        trigger={
                          <Button className="rounded-full">
                            <CalendarPlus className="size-4" /> Book Appointment
                          </Button>
                        }
                      />
                    ) : undefined
                  }
                />
              }
            />
          )}
        </section>
      )}

      {/* The desk that takes payments sees what is waiting for one. */}
      <InvoicesAwaitingPayment />
    </div>
  )
}
