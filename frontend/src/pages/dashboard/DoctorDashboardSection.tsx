import { CalendarClock, CalendarRange, Users } from 'lucide-react'
import { isMissingDoctorProfile, useDoctorDashboard } from '@/api/reports'
import { AppointmentStatusBadge } from '@/components/appointments/AppointmentStatusBadge'
import { Alert } from '@/components/ui/alert'
import { StatTile } from '@/components/ui/stat-tile'
import { usePermissions } from '@/hooks/usePermissions'
import { formatCount, formatTimeIn } from '@/lib/format'
import { DashboardSection } from './DashboardSection'
import { dashboardSectionsFor, TILE_GRID } from './dashboardSections'
import { PatientName } from './PatientName'

/**
 * The signed-in doctor's own day, for holders of `report.doctor.read` who do
 * not see the Admin section. Every figure is a field of
 * `GET /dashboards/doctor`; it carries no money, and `to_see` is the server's
 * own sum. Times are on the hospital's clock (`meta.timezone`).
 *
 * An account with the code but no doctor profile gets a 404: that is an
 * answer, not a fault, so it is shown as a note with nothing to retry.
 */
export function DoctorDashboardSection() {
  const { can } = usePermissions()
  const enabled = dashboardSectionsFor(can).doctor
  const query = useDoctorDashboard({ enabled })
  if (!enabled) return null

  if (isMissingDoctorProfile(query.error)) {
    return (
      <Alert variant="info">
        No doctor profile is linked to your account, so there is no personal schedule to show.
      </Alert>
    )
  }

  const data = query.data
  const isLoading = !data && query.isPending
  const schedule = data?.schedule_today
  const week = data?.this_week

  return (
    <DashboardSection
      title="My day"
      name="doctor"
      isError={!data && query.isError}
      isStale={Boolean(data) && query.isError}
      onRetry={() => void query.refetch()}
    >
      <div className={TILE_GRID[3]}>
        <StatTile
          label="My appointments today"
          icon={CalendarClock}
          value={schedule && formatCount(schedule.total)}
          hint={
            schedule &&
            `${formatCount(schedule.completed)} done · ${formatCount(schedule.to_see)} to see`
          }
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="My patients"
          icon={Users}
          value={data && formatCount(data.my_patients.count)}
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="My week"
          icon={CalendarRange}
          value={week && formatCount(week.total)}
          hint={
            week &&
            `${formatCount(week.completed)} completed · ${formatCount(week.no_show)} no-show · ${formatCount(week.cancelled)} cancelled`
          }
          isLoading={isLoading}
          isError={false}
        />
      </div>

      {data && schedule && (
        <div className="neo-extruded bg-surface min-w-0 rounded-2xl p-5">
          <h3 className="font-label text-label-caps text-on-surface-variant">Today's schedule</h3>
          {schedule.appointments.length === 0 ? (
            <p className="font-body text-body-sm text-on-surface-variant mt-2">
              You have no appointments today.
            </p>
          ) : (
            <ul className="divide-outline-variant/20 font-body text-body-sm mt-2 divide-y">
              {schedule.appointments.map((a) => (
                <li
                  key={a.appointment_id}
                  className="flex flex-wrap items-center gap-x-4 gap-y-1 py-2"
                >
                  <span className="font-mono">
                    {formatTimeIn(a.scheduled_start, data.meta.timezone)}
                  </span>
                  <span className="min-w-0 flex-1 break-words">
                    <PatientName id={a.patient_id} name={a.patient_name} />
                  </span>
                  <span className="text-on-surface-variant font-mono">{a.patient_mrn}</span>
                  <AppointmentStatusBadge status={a.status} />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </DashboardSection>
  )
}
