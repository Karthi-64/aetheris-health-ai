import { Link } from 'react-router-dom'
import { ArrowRight, CalendarClock, CalendarPlus, Stethoscope, UserPlus, Users } from 'lucide-react'
import { KpiCard } from '@/components/ui/kpi-card'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { useAuthStore } from '@/store/auth-store'
import { usePermissions } from '@/hooks/usePermissions'
import { usePatients } from '@/api/patients'
import { useDoctors } from '@/api/doctors'
import { useAppointments } from '@/api/appointments'
import { todayISODate } from '@/lib/format'
import { RegisterPatientDialog } from '@/pages/patients/RegisterPatientDialog'
import { BookAppointmentDialog } from '@/pages/appointments/BookAppointmentDialog'
import { appointmentColumns } from '@/pages/appointments/columns'

/** A KPI backed by a real count — a skeleton while loading, "—" if it can't be read. */
function StatTile({
  label,
  icon,
  total,
  isLoading,
  isError,
}: {
  label: string
  icon: typeof Users
  total: number | undefined
  isLoading: boolean
  isError: boolean
}) {
  if (isLoading) return <Skeleton className="h-[104px] rounded-2xl" />
  return <KpiCard label={label} icon={icon} value={isError || total === undefined ? '—' : total} />
}

/** Grid classes by how many KPI tiles the user may see (static, so Tailwind keeps them). */
const TILE_GRID: Record<number, string> = {
  1: 'grid gap-5 sm:grid-cols-1',
  2: 'grid gap-5 sm:grid-cols-2',
  3: 'grid gap-5 sm:grid-cols-3',
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
  const today = todayISODate()

  const patients = usePatients({ page: 1, page_size: 1 }, { enabled: canSeePatients })
  const doctors = useDoctors({ page: 1, page_size: 1 }, { enabled: canSeeDoctors })
  const appts = useAppointments(
    { appointment_date: today, page: 1, page_size: 25 },
    { enabled: canSeeAppointments },
  )
  const tileCount = [canSeeAppointments, canSeePatients, canSeeDoctors].filter(Boolean).length

  const todaysAppointments = appts.data?.items ?? []

  return (
    <div className="w-full space-y-6">
      {/* Greeting + quick actions */}
      <div className="neo-extruded bg-surface flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6 md:p-8">
        <div>
          <h1 className="font-display text-primary text-2xl font-bold md:text-headline-lg">
            Good morning, {name}
          </h1>
          <p className="font-body text-body-sm text-on-surface-variant mt-1 max-w-xl">
            Your operations hub — register patients, book appointments, and work the day's queue.
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

      {/* Real KPIs — only the ones this user may read */}
      {tileCount > 0 && (
        <div className={TILE_GRID[tileCount]}>
          {canSeeAppointments && (
            <StatTile
              label="Today's appointments"
              icon={CalendarClock}
              total={appts.data?.pagination.total}
              isLoading={appts.isPending}
              isError={appts.isError}
            />
          )}
          {canSeePatients && (
            <StatTile
              label="Total patients"
              icon={Users}
              total={patients.data?.pagination.total}
              isLoading={patients.isPending}
              isError={patients.isError}
            />
          )}
          {canSeeDoctors && (
            <StatTile
              label="Doctors"
              icon={Stethoscope}
              total={doctors.data?.pagination.total}
              isLoading={doctors.isPending}
              isError={doctors.isError}
            />
          )}
        </div>
      )}

      {/* Today's queue */}
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
              columns={appointmentColumns}
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
    </div>
  )
}
