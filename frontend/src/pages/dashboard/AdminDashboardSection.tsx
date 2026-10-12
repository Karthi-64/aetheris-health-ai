import { CalendarClock, Receipt, UserPlus } from 'lucide-react'
import { useAdminDashboard } from '@/api/reports'
import { Alert } from '@/components/ui/alert'
import { StatTile } from '@/components/ui/stat-tile'
import { usePermissions } from '@/hooks/usePermissions'
import { formatCount, formatMoney } from '@/lib/format'
import { DashboardSection } from './DashboardSection'
import { dashboardSectionsFor, TILE_GRID } from './dashboardSections'

/**
 * The hospital-wide tiles, for holders of `report.admin.read`. Every figure is
 * a field of `GET /dashboards/admin`, in the hospital's timezone and currency;
 * nothing is added up here (`in_clinic` is the server's own sum).
 */
export function AdminDashboardSection() {
  const { can } = usePermissions()
  const enabled = dashboardSectionsFor(can).admin
  const query = useAdminDashboard({ enabled })
  if (!enabled) return null

  const data = query.data
  const isLoading = !data && query.isPending
  const money = (value: string) => formatMoney(value, data?.meta.currency)
  const today = data?.appointments_today
  const week = data?.revenue_this_week
  const month = data?.patient_registrations_this_month

  return (
    <DashboardSection
      title="Hospital overview"
      name="admin"
      isError={!data && query.isError}
      isStale={Boolean(data) && query.isError}
      onRetry={() => void query.refetch()}
    >
      {month?.active_total === 0 && (
        <Alert variant="info" title="Nothing to report yet">
          Figures appear here as soon as your team registers patients, books appointments and issues
          invoices. Start with Register Patient or Book Appointment above.
        </Alert>
      )}
      <div className={TILE_GRID[3]}>
        <StatTile
          label="Today's appointments"
          icon={CalendarClock}
          value={today && formatCount(today.total)}
          hint={
            today &&
            `${formatCount(today.completed)} completed · ${formatCount(today.in_clinic)} in clinic · ${formatCount(today.booked)} to come`
          }
          to={can('appointment.read') ? '/appointments' : undefined}
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="Billed this week"
          icon={Receipt}
          value={week && money(week.invoiced_amount)}
          hint={
            week && `Collected ${money(week.collected_amount)} · Refunded ${money(week.refunded_amount)}`
          }
          to="/reports/revenue"
          isLoading={isLoading}
          isError={false}
        />
        <StatTile
          label="New patients this month"
          icon={UserPlus}
          value={month && formatCount(month.registered)}
          hint={month && `${formatCount(month.active_total)} active patients`}
          to="/reports/patients"
          isLoading={isLoading}
          isError={false}
        />
      </div>
    </DashboardSection>
  )
}
