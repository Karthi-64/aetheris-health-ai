import type { ColumnDef } from '@tanstack/react-table'
import { UserCheck, UserPlus } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { BarChart } from '@/components/charts'
import { ChartCard } from '@/components/charts/ChartCard'
import { DataTable } from '@/components/ui/data-table'
import { StatTile } from '@/components/ui/stat-tile'
import { usePatientsReport } from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import { formatBucketLabel, formatCount } from '@/lib/format'
import { ReportExportButton } from './ReportExportButton'
import { ReportFilters } from './ReportFilters'
import { ReportsTabs } from './ReportsTabs'
import { figureColumn, periodColumn, type PeriodRow } from './reportColumns'
import { KpiGrid, ReportDenied, ReportError, ReportMetaLine } from './reportParts'
import { GRANULARITY_NOUNS, PERIOD_TABLE_PAGE_SIZE, periodText } from './reportPresentation'
import { useLastToday, useReportFilters } from './useReportFilters'

const EMPTY = 'No patients were registered in this period.'

interface Row extends PeriodRow {
  registered: string
}

const columns: ColumnDef<Row>[] = [periodColumn<Row>(), figureColumn<Row>('registered', 'Registered')]

/**
 * Patients registered per period (`GET /reports/patients`, which needs
 * `report.admin.read`). Every figure is the server's, counted in the
 * hospital's timezone: the tiles show its summary, the Total row is that same
 * summary, and nothing is added up here. Deactivated patients are not counted.
 */
export default function PatientsReportPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own: it is not
  // made for someone the endpoint would refuse.
  const canRead = can('report.admin.read')
  const filters = useReportFilters()
  const query = usePatientsReport(filters.params, { enabled: canRead && !filters.error })
  // Figures kept from the previous period while the next one loads would sit
  // under filters they do not belong to, so they are not shown.
  const data = query.isPlaceholderData ? undefined : query.data
  const today = useLastToday(query.data?.meta.today)

  if (!canRead) return <ReportDenied />

  const isLoading = !data
  const granularity = data?.filters.granularity ?? 'day'
  const summary = data?.summary
  const isEmpty = summary?.registered === 0

  const rows: Row[] = data
    ? [
        ...data.buckets.map((bucket) => ({
          label: formatBucketLabel(bucket, granularity),
          partial: bucket.partial,
          isTotal: false,
          registered: formatCount(bucket.registered),
        })),
        { label: 'Total', partial: false, isTotal: true, registered: formatCount(data.summary.registered) },
      ]
    : []

  return (
    <div className="w-full">
      <ReportsTabs />
      <PageHeader
        title="Patients report"
        subtitle="New patient registrations over a period, and how many patients are active now."
        actions={
          <ReportExportButton
            reportId="patients"
            params={filters.params}
            period={data?.filters}
            disabled={!!filters.error}
          />
        }
      />
      <ReportFilters filters={filters} resolved={data?.filters} today={today} />

      {filters.error ? null : query.isError ? (
        <ReportError
          error={query.error}
          onRetry={() => query.refetch()}
          onClearFilters={filters.isFiltered ? filters.clear : undefined}
        />
      ) : (
        <>
          <ReportMetaLine meta={data?.meta} />
          <KpiGrid>
            <StatTile
              label="Registered in period"
              icon={UserPlus}
              value={summary && formatCount(summary.registered)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Active patients"
              icon={UserCheck}
              value={summary && formatCount(summary.active_total)}
              hint="As of now, whatever the period"
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Female"
              value={summary && formatCount(summary.by_gender.female)}
              hint="Registered in period"
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Male"
              value={summary && formatCount(summary.by_gender.male)}
              hint="Registered in period"
              isLoading={isLoading}
              isError={false}
            />
            {summary && summary.by_gender.other !== 0 && (
              <StatTile
                label="Other"
                value={formatCount(summary.by_gender.other)}
                hint="Registered in period"
                isLoading={false}
                isError={false}
              />
            )}
            {summary && summary.by_gender.unspecified !== 0 && (
              <StatTile
                label="Unspecified"
                value={formatCount(summary.by_gender.unspecified)}
                hint="Registered in period"
                isLoading={false}
                isError={false}
              />
            )}
          </KpiGrid>

          <ChartCard
            title="Registrations"
            description={data && `Per ${GRANULARITY_NOUNS[granularity]}, ${periodText(data.filters)}`}
            isLoading={isLoading}
            isError={false}
            onRetry={() => query.refetch()}
            isEmpty={isEmpty}
            emptyTitle={EMPTY}
            summary={
              data
                ? `Patients registered per ${GRANULARITY_NOUNS[granularity]}, ${periodText(data.filters)}: ${formatCount(data.summary.registered)} in total. The table below has every period.`
                : ''
            }
          >
            <BarChart
              data={(data?.buckets ?? []).map((bucket) => ({
                period: formatBucketLabel(bucket, granularity),
                registered: bucket.registered,
              }))}
              xKey="period"
              series={[{ key: 'registered', label: 'Registered' }]}
            />
          </ChartCard>

          {!isEmpty && (
            <DataTable
              className="mt-6"
              columns={columns}
              data={rows}
              isLoading={isLoading}
              pageSize={PERIOD_TABLE_PAGE_SIZE}
            />
          )}
        </>
      )}
    </div>
  )
}
