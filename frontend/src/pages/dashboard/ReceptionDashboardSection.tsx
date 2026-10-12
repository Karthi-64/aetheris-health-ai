import { CalendarClock, Footprints, UserX } from 'lucide-react'
import { useReceptionDashboard } from '@/api/reports'
import { StatTile } from '@/components/ui/stat-tile'
import { usePermissions } from '@/hooks/usePermissions'
import { formatCount, formatTimeIn } from '@/lib/format'
import { DashboardSection } from './DashboardSection'
import { dashboardSectionsFor, TILE_GRID } from './dashboardSections'
import { PatientName } from './PatientName'

/**
 * The front desk's day, for holders of `report.reception.read` who do not see
 * the Admin section. Every figure is a field of `GET /dashboards/reception`;
 * it carries no money. Times are on the hospital's clock (`meta.timezone`).
 */
export function ReceptionDashboardSection() {
  const { can } = usePermissions()
  const enabled = dashboardSectionsFor(can).reception
  const query = useReceptionDashboard({ enabled })
  if (!enabled) return null

  const data = query.data
  const isLoading = !data && query.isPending
  const schedule = data?.schedule_today
  const walkIns = data?.walk_in_queue
  const alerts = data?.no_show_alerts
  const atRisk = alerts?.at_risk_appointments ?? []

  return (
    <DashboardSection
      title="Front desk"
      name="reception"
      isError={!data && query.isError}
      isStale={Boolean(data) && query.isError}
      onRetry={() => void query.refetch()}
    >
      <div className={TILE_GRID[3]}>
        <StatTile
          label="Today's appointments"
          icon={CalendarClock}
          value={schedule && formatCount(schedule.total)}
          hint={
            schedule &&
            `${formatCount(schedule.completed)} completed · ${formatCount(schedule.in_clinic)} in clinic · ${formatCount(schedule.booked)} to come`
          }
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="Walk-ins waiting"
          icon={Footprints}
          value={walkIns && formatCount(walkIns.waiting)}
          hint={
            walkIns &&
            [
              ...(walkIns.longest_wait_minutes === null
                ? []
                : [`Longest wait ${formatCount(walkIns.longest_wait_minutes)} min`]),
              `${formatCount(walkIns.not_arrived)} not arrived`,
            ].join(' · ')
          }
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="Possible no-shows"
          icon={UserX}
          value={alerts && formatCount(alerts.at_risk)}
          hint={alerts && `${formatCount(alerts.marked_today)} marked no-show today`}
          isLoading={isLoading}
          isError={false}
        />
      </div>

      {data && alerts && atRisk.length > 0 && (
        <div className="neo-extruded bg-surface min-w-0 rounded-2xl p-5">
          <h3 className="font-label text-label-caps text-on-surface-variant">
            Booked, past their time and not checked in
          </h3>
          <ul className="divide-outline-variant/20 font-body text-body-sm mt-2 divide-y">
            {atRisk.map((a) => (
              <li
                key={a.appointment_id}
                className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-2"
              >
                <span className="font-mono">{formatTimeIn(a.scheduled_start, data.meta.timezone)}</span>
                <span className="min-w-0 flex-1 break-words">
                  <PatientName id={a.patient_id} name={a.patient_name} />
                </span>
                <span className="text-on-surface-variant break-words">{a.doctor_name}</span>
                <span className="text-critical font-semibold">
                  {formatCount(a.minutes_late)} min late
                </span>
              </li>
            ))}
          </ul>
          {alerts.at_risk > atRisk.length && (
            <p className="font-body text-body-sm text-on-surface-variant mt-2">
              Showing the first {formatCount(atRisk.length)} of {formatCount(alerts.at_risk)}.
            </p>
          )}
        </div>
      )}
    </DashboardSection>
  )
}
