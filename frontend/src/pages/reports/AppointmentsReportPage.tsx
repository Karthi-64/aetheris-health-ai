import type { ColumnDef } from '@tanstack/react-table'
import { Link, useSearchParams } from 'react-router-dom'
import { CalendarCheck, CalendarDays, CalendarOff, CalendarX } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { BarChart } from '@/components/charts'
import { ChartCard } from '@/components/charts/ChartCard'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { StatTile } from '@/components/ui/stat-tile'
import { useAppointmentsReport, type AppointmentCounts } from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import { formatBucketLabel, formatCount } from '@/lib/format'
import { ReportExportButton } from './ReportExportButton'
import { ReportFilters } from './ReportFilters'
import { ReportsTabs } from './ReportsTabs'
import { figureColumn, periodColumn, type PeriodRow } from './reportColumns'
import { KpiGrid, ReportDenied, ReportError, ReportMetaLine, SectionHeading } from './reportParts'
import {
  APPOINTMENT_SERIES,
  GRANULARITY_NOUNS,
  PERIOD_TABLE_PAGE_SIZE,
  periodText,
} from './reportPresentation'
import { useLastToday, useReportFilters } from './useReportFilters'

const EMPTY = 'No appointments were scheduled in this period.'
const UNASSIGNED = 'Unassigned'

type CountStrings = Record<keyof AppointmentCounts, string>

const countStrings = (counts: AppointmentCounts): CountStrings => ({
  total: formatCount(counts.total),
  booked: formatCount(counts.booked),
  checked_in: formatCount(counts.checked_in),
  in_progress: formatCount(counts.in_progress),
  completed: formatCount(counts.completed),
  cancelled: formatCount(counts.cancelled),
  no_show: formatCount(counts.no_show),
})

type PeriodCounts = PeriodRow & CountStrings

const periodColumns: ColumnDef<PeriodCounts>[] = [
  periodColumn<PeriodCounts>(),
  figureColumn<PeriodCounts>('total', 'Total'),
  figureColumn<PeriodCounts>('booked', 'Booked'),
  figureColumn<PeriodCounts>('checked_in', 'Checked in'),
  figureColumn<PeriodCounts>('in_progress', 'In progress'),
  figureColumn<PeriodCounts>('completed', 'Completed'),
  figureColumn<PeriodCounts>('cancelled', 'Cancelled'),
  figureColumn<PeriodCounts>('no_show', 'No-show'),
]

interface DoctorRow {
  doctorId: string
  doctor: string
  department: string
  total: string
  completed: string
  cancelled: string
  no_show: string
}

/**
 * A doctor's name as a link that narrows the report to that doctor. The list
 * the Doctor select offers can miss a doctor; every doctor with appointments
 * is reachable from here.
 */
function DoctorLink({ doctorId, name }: { doctorId: string; name: string }) {
  const [searchParams] = useSearchParams()
  const next = new URLSearchParams(searchParams)
  next.set('doctor_id', doctorId)
  return (
    <Link
      to={{ search: next.toString() }}
      replace
      aria-label={`Show only ${name}`}
      className="text-on-surface hover:text-secondary focus-visible:ring-secondary rounded font-semibold transition-colors outline-none focus-visible:ring-2"
    >
      {name}
    </Link>
  )
}

const doctorColumns: ColumnDef<DoctorRow>[] = [
  {
    id: 'doctor',
    header: 'Doctor',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="block max-w-64 min-w-32 [overflow-wrap:anywhere]">
        <DoctorLink doctorId={row.original.doctorId} name={row.original.doctor} />
      </span>
    ),
  },
  {
    id: 'department',
    header: 'Department',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant block max-w-48 [overflow-wrap:anywhere]">
        {row.original.department}
      </span>
    ),
  },
  figureColumn<DoctorRow>('total', 'Total'),
  figureColumn<DoctorRow>('completed', 'Completed'),
  figureColumn<DoctorRow>('cancelled', 'Cancelled'),
  figureColumn<DoctorRow>('no_show', 'No-show'),
]

interface DepartmentRow {
  department: string
  total: string
  completed: string
  cancelled: string
  no_show: string
}

const departmentColumns: ColumnDef<DepartmentRow>[] = [
  {
    id: 'department',
    header: 'Department',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="block max-w-64 min-w-32 font-semibold [overflow-wrap:anywhere]">
        {row.original.department}
      </span>
    ),
  },
  figureColumn<DepartmentRow>('total', 'Total'),
  figureColumn<DepartmentRow>('completed', 'Completed'),
  figureColumn<DepartmentRow>('cancelled', 'Cancelled'),
  figureColumn<DepartmentRow>('no_show', 'No-show'),
]

/**
 * Appointments over a period (`GET /reports/appointments`, which needs
 * `report.admin.read`), by status, by doctor and by department. An appointment
 * is counted on the day it was scheduled for, in the hospital's timezone —
 * a cancellation or a no-show too, not on the day it was marked.
 *
 * Every figure, the totals and the no-show rate are the server's. The rate is
 * no-shows as a share of appointments that reached an outcome (completed or
 * no-show); the server sends none when no appointment has.
 */
export default function AppointmentsReportPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own.
  const canRead = can('report.admin.read')
  const filters = useReportFilters({ doctor: true, department: true })
  const query = useAppointmentsReport(filters.params, { enabled: canRead && !filters.error })
  // Figures of the previous filters are not shown under the new ones.
  const data = query.isPlaceholderData ? undefined : query.data
  const today = useLastToday(query.data?.meta.today)

  if (!canRead) return <ReportDenied />

  const isLoading = !data
  const granularity = data?.filters.granularity ?? 'day'
  const summary = data?.summary
  const isEmpty = summary?.total === 0

  const periodRows: PeriodCounts[] = data
    ? [
        ...data.buckets.map((bucket) => ({
          label: formatBucketLabel(bucket, granularity),
          partial: bucket.partial,
          isTotal: false,
          ...countStrings(bucket),
        })),
        { label: 'Total', partial: false, isTotal: true, ...countStrings(data.summary) },
      ]
    : []
  const doctorRows: DoctorRow[] = (data?.by_doctor ?? []).map((row) => ({
    doctorId: row.doctor_id,
    doctor: row.doctor_name,
    department: row.department_name ?? UNASSIGNED,
    total: formatCount(row.total),
    completed: formatCount(row.completed),
    cancelled: formatCount(row.cancelled),
    no_show: formatCount(row.no_show),
  }))
  const departmentRows: DepartmentRow[] = (data?.by_department ?? []).map((row) => ({
    department: row.department_name ?? UNASSIGNED,
    total: formatCount(row.total),
    completed: formatCount(row.completed),
    cancelled: formatCount(row.cancelled),
    no_show: formatCount(row.no_show),
  }))

  const scope = data
    ? [data.filters.doctor?.name, data.filters.department?.name].filter(Boolean).join(', ')
    : ''

  return (
    <div className="w-full">
      <ReportsTabs />
      <PageHeader
        title="Appointments report"
        subtitle="Appointments by status over a period, counted on the day each was scheduled for."
        actions={
          <ReportExportButton
            reportId="appointments"
            params={filters.params}
            period={data?.filters}
            disabled={!!filters.error}
          />
        }
      />
      <ReportFilters
        filters={filters}
        resolved={data?.filters}
        today={today}
        department={{ echoed: data?.filters.department }}
        doctor={{ echoed: data?.filters.doctor }}
      />

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
              label="Total"
              icon={CalendarDays}
              value={summary && formatCount(summary.total)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Completed"
              icon={CalendarCheck}
              value={summary && formatCount(summary.completed)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Cancelled"
              icon={CalendarX}
              value={summary && formatCount(summary.cancelled)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="No-show"
              icon={CalendarOff}
              value={summary && formatCount(summary.no_show)}
              hint={
                summary && summary.no_show_rate_percent !== null
                  ? `${summary.no_show_rate_percent}% of appointments that were due`
                  : undefined
              }
              isLoading={isLoading}
              isError={false}
            />
          </KpiGrid>

          <ChartCard
            title="Appointments by status"
            description={
              data &&
              `Per ${GRANULARITY_NOUNS[granularity]}, ${periodText(data.filters)}${scope ? ` · ${scope}` : ''}`
            }
            isLoading={isLoading}
            isError={false}
            onRetry={() => query.refetch()}
            isEmpty={isEmpty}
            emptyTitle={EMPTY}
            summary={
              data
                ? `Appointments per ${GRANULARITY_NOUNS[granularity]} by status, ${periodText(data.filters)}: ${formatCount(data.summary.total)} in total, ${formatCount(data.summary.completed)} completed, ${formatCount(data.summary.cancelled)} cancelled and ${formatCount(data.summary.no_show)} no-show. The table below has every period.`
                : ''
            }
          >
            <BarChart
              stacked
              data={(data?.buckets ?? []).map((bucket) => ({
                period: formatBucketLabel(bucket, granularity),
                completed: bucket.completed,
                booked: bucket.booked,
                checked_in: bucket.checked_in,
                in_progress: bucket.in_progress,
                cancelled: bucket.cancelled,
                no_show: bucket.no_show,
              }))}
              xKey="period"
              series={APPOINTMENT_SERIES}
            />
          </ChartCard>

          {!isEmpty && (
            <>
              <DataTable
                className="mt-6"
                columns={periodColumns}
                data={periodRows}
                isLoading={isLoading}
                pageSize={PERIOD_TABLE_PAGE_SIZE}
              />

              <SectionHeading>By doctor</SectionHeading>
              <DataTable
                columns={doctorColumns}
                data={doctorRows}
                isLoading={isLoading}
                pageSize={25}
                emptyState={<EmptyState title="No doctor had appointments in this period." className="py-10" />}
              />

              <SectionHeading>By department</SectionHeading>
              <p className="font-body text-body-sm text-on-surface-variant mb-3">
                By each doctor's current department. Doctors with none are listed as {UNASSIGNED}.
              </p>
              <DataTable
                columns={departmentColumns}
                data={departmentRows}
                isLoading={isLoading}
                pageSize={25}
                emptyState={
                  <EmptyState title="No department had appointments in this period." className="py-10" />
                }
              />
            </>
          )}
        </>
      )}
    </div>
  )
}
