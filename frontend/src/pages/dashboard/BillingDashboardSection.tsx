import { BadgePercent, FileClock, Receipt } from 'lucide-react'
import { useBillingDashboard, type DateRange, type RevenueFigures } from '@/api/reports'
import { StatTile } from '@/components/ui/stat-tile'
import { usePermissions } from '@/hooks/usePermissions'
import { formatCount, formatMoney } from '@/lib/format'
import { DashboardSection } from './DashboardSection'
import { dashboardSectionsFor, TILE_GRID } from './dashboardSections'

/**
 * Unpaid invoices, billed amounts and discounts awaiting approval, for holders
 * of `report.billing.read`. Every figure is a field of
 * `GET /dashboards/billing`, in the hospital's currency.
 */
export function BillingDashboardSection() {
  const { can } = usePermissions()
  const enabled = dashboardSectionsFor(can).billing
  const query = useBillingDashboard({ enabled })
  if (!enabled) return null

  const data = query.data
  const isLoading = !data && query.isPending
  const money = (value: string) => formatMoney(value, data?.meta.currency)
  const unpaid = data?.unpaid_invoices
  const discounts = data?.discounts_pending_approval

  const billed = (label: string, figures: (DateRange & RevenueFigures) | undefined) => (
    <StatTile
      label={label}
      icon={Receipt}
      value={figures && money(figures.invoiced_amount)}
      hint={figures && `Collected ${money(figures.collected_amount)}`}
      to="/reports/revenue"
      isLoading={isLoading}
      isError={false}
    />
  )

  return (
    <DashboardSection
      title="Billing"
      name="billing"
      isError={!data && query.isError}
      isStale={Boolean(data) && query.isError}
      onRetry={() => void query.refetch()}
    >
      <div className={TILE_GRID[5]}>
        <StatTile
          label="Unpaid invoices"
          icon={FileClock}
          value={unpaid && formatCount(unpaid.invoice_count)}
          hint={unpaid && `${money(unpaid.outstanding_amount)} outstanding`}
          to="/reports/outstanding"
          isLoading={isLoading}
          isError={false}
        />
        {billed('Billed today', data?.revenue.today)}
        {billed('Billed this week', data?.revenue.this_week)}
        {billed('Billed this month', data?.revenue.this_month)}
        <StatTile
          label="Discounts awaiting approval"
          icon={BadgePercent}
          value={discounts && formatCount(discounts.invoice_count)}
          hint={discounts && `${money(discounts.discount_amount)} in discounts`}
          isLoading={isLoading}
          isError={false}
        />
      </div>
    </DashboardSection>
  )
}
