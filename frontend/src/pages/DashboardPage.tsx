import { Link } from 'react-router-dom'
import { ArrowRight, CalendarClock, CalendarPlus, Stethoscope, UserPlus, Users } from 'lucide-react'
import { KpiCard } from '@/components/ui/kpi-card'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { useAuthStore } from '@/store/auth-store'
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

export default function DashboardPage() {
  const name = useAuthStore((s) => s.user?.name) ?? 'there'
  const today = todayISODate()

  const patients = usePatients({ page: 1, page_size: 1 })
  const doctors = useDoctors({ page: 1, page_size: 1 })
  const appts = useAppointments({ appointment_date: today, page: 1, page_size: 25 })

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
          <RegisterPatientDialog
            trigger={
              <Button variant="outline" className="rounded-full">
                <UserPlus className="size-4" /> Register Patient
              </Button>
            }
          />
          <BookAppointmentDialog
            trigger={
              <Button className="rounded-full">
                <CalendarPlus className="size-4" /> Book Appointment
              </Button>
            }
          />
        </div>
      </div>

      {/* Real KPIs */}
      <div className="grid gap-5 sm:grid-cols-3">
        <StatTile
          label="Today's appointments"
          icon={CalendarClock}
          total={appts.data?.pagination.total}
          isLoading={appts.isPending}
          isError={appts.isError}
        />
        <StatTile
          label="Total patients"
          icon={Users}
          total={patients.data?.pagination.total}
          isLoading={patients.isPending}
          isError={patients.isError}
        />
        <StatTile
          label="Doctors"
          icon={Stethoscope}
          total={doctors.data?.pagination.total}
          isLoading={doctors.isPending}
          isError={doctors.isError}
        />
      </div>

      {/* Today's queue */}
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
                  <BookAppointmentDialog
                    trigger={
                      <Button className="rounded-full">
                        <CalendarPlus className="size-4" /> Book Appointment
                      </Button>
                    }
                  />
                }
              />
            }
          />
        )}
      </section>
    </div>
  )
}
