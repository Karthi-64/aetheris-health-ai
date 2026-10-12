import type { ColumnDef } from '@tanstack/react-table'
import { Banknote, FileText, Undo2, Wallet } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { BarChart } from '@/components/charts'
import { ChartCard } from '@/components/charts/ChartCard'
import { DataTable } from '@/components/ui/data-table'
import { StatTile } from '@/components/ui/stat-tile'
import { useRevenueReport, type RevenueFigures } from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import { formatBucketLabel, formatCount, formatMoney, formatMoneyCompact } from '@/lib/format'
import { ReportExportButton } from './ReportExportButton'
import { ReportFilters } from './ReportFilters'
import { ReportsTabs } from './ReportsTabs'
import { figureColumn, periodColumn, type PeriodRow } from './reportColumns'
import { KpiGrid, ReportDenied, ReportError, ReportMetaLine, SectionHeading } from './reportParts'
import {
  GRANULARITY_NOUNS,
  PAYMENT_METHOD_LABELS,
  PERIOD_TABLE_PAGE_SIZE,
  countOf,
  periodText,
} from './reportPresentation'
import { REPORT_READ_CODES, holdsAny } from './reportSections'
import { useLastToday, useReportFilters } from './useReportFilters'

const EMPTY = 'No invoices, payments or refunds in this period.'

interface PeriodRevenue extends PeriodRow {
  invoices: string
  billed: string
  collected: string
  refunded: string
  net: string
}

const figuresOf = (figures: RevenueFigures, currency: string) => ({
  invoices: formatCount(figures.invoice_count),
  billed: formatMoney(figures.invoiced_amount, currency),
  collected: formatMoney(figures.collected_amount, currency),
  refunded: formatMoney(figures.refunded_amount, currency),
  net: formatMoney(figures.net_collected_amount, currency),
})

const periodColumns: ColumnDef<PeriodRevenue>[] = [
  periodColumn<PeriodRevenue>(),
  figureColumn<PeriodRevenue>('invoices', 'Invoices'),
  figureColumn<PeriodRevenue>('billed', 'Billed'),
  figureColumn<PeriodRevenue>('collected', 'Collected'),
  figureColumn<PeriodRevenue>('refunded', 'Refunded'),
  figureColumn<PeriodRevenue>('net', 'Net collected'),
]

interface MethodRow {
  method: string
  payments: string
  collected: string
  refunds: string
  refunded: string
  net: string
}

const methodColumns: ColumnDef<MethodRow>[] = [
  {
    id: 'method',
    header: 'Method',
    enableSorting: false,
    cell: ({ row }) => <span className="font-semibold whitespace-nowrap">{row.original.method}</span>,
  },
  figureColumn<MethodRow>('payments', 'Payments'),
  figureColumn<MethodRow>('collected', 'Collected'),
  figureColumn<MethodRow>('refunds', 'Refunds'),
  figureColumn<MethodRow>('refunded', 'Refunded'),
  figureColumn<MethodRow>('net', 'Net collected'),
]

/**
 * Billed, collected and refunded money over a period (`GET /reports/revenue`,
 * which needs `report.admin.read` or `report.billing.read`), in the hospital's
 * currency and timezone.
 *
 * Every amount — the net included — is the server's, shown as sent; nothing is
 * added or subtracted here. Billed and collected are dated differently (see
 * the footnote), so this page does not set one against the other as a rate.
 */
export default function RevenueReportPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own.
  const canRead = holdsAny(can, REPORT_READ_CODES.revenue)
  const filters = useReportFilters()
  const query = useRevenueReport(filters.params, { enabled: canRead && !filters.error })
  // Figures of the previous period are not shown under the new one.
  const data = query.isPlaceholderData ? undefined : query.data
  const today = useLastToday(query.data?.meta.today)

  if (!canRead) return <ReportDenied />

  const isLoading = !data
  const granularity = data?.filters.granularity ?? 'day'
  const currency = data?.meta.currency
  const summary = data?.summary
  const money = (value: string) => formatMoney(value, currency)
  const isEmpty =
    !!summary && summary.invoice_count === 0 && summary.payment_count === 0 && summary.refund_count === 0

  const periodRows: PeriodRevenue[] = data
    ? [
        ...data.buckets.map((bucket) => ({
          label: formatBucketLabel(bucket, granularity),
          partial: bucket.partial,
          isTotal: false,
          ...figuresOf(bucket, data.meta.currency),
        })),
        { label: 'Total', partial: false, isTotal: true, ...figuresOf(data.summary, data.meta.currency) },
      ]
    : []
  const methodRows: MethodRow[] = (data?.by_method ?? []).map((row) => ({
    method: PAYMENT_METHOD_LABELS[row.method] ?? row.method,
    payments: formatCount(row.payment_count),
    collected: money(row.collected_amount),
    refunds: formatCount(row.refund_count),
    refunded: money(row.refunded_amount),
    net: money(row.net_collected_amount),
  }))

  return (
    <div className="w-full">
      <ReportsTabs />
      <PageHeader
        title="Revenue report"
        subtitle="What was billed, collected and refunded over a period."
        actions={
          <ReportExportButton
            reportId="revenue"
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
          <ReportMetaLine meta={data?.meta} withCurrency />
          <KpiGrid>
            <StatTile
              label="Billed"
              icon={FileText}
              value={summary && money(summary.invoiced_amount)}
              hint={summary && countOf(summary.invoice_count, 'invoice', 'invoices')}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Collected"
              icon={Banknote}
              value={summary && money(summary.collected_amount)}
              hint={summary && countOf(summary.payment_count, 'payment', 'payments')}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Refunded"
              icon={Undo2}
              value={summary && money(summary.refunded_amount)}
              hint={summary && countOf(summary.refund_count, 'refund', 'refunds')}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Net collected"
              icon={Wallet}
              value={summary && money(summary.net_collected_amount)}
              hint="Collected less refunded"
              isLoading={isLoading}
              isError={false}
            />
          </KpiGrid>

          <ChartCard
            title="Billed, collected and refunded"
            description={data && `Per ${GRANULARITY_NOUNS[granularity]}, ${periodText(data.filters)}`}
            isLoading={isLoading}
            isError={false}
            onRetry={() => query.refetch()}
            isEmpty={isEmpty}
            emptyTitle={EMPTY}
            summary={
              data
                ? `Revenue per ${GRANULARITY_NOUNS[granularity]}, ${periodText(data.filters)}: ${money(data.summary.invoiced_amount)} billed, ${money(data.summary.collected_amount)} collected, ${money(data.summary.refunded_amount)} refunded and ${money(data.summary.net_collected_amount)} net collected. The table below has every period.`
                : ''
            }
          >
            <BarChart
              // The amounts are decimal strings; they become numbers only to
              // set the height of a bar. Every figure a user reads is the string.
              data={(data?.buckets ?? []).map((bucket) => ({
                period: formatBucketLabel(bucket, granularity),
                billed: Number(bucket.invoiced_amount),
                collected: Number(bucket.collected_amount),
                refunded: Number(bucket.refunded_amount),
              }))}
              xKey="period"
              series={[
                { key: 'billed', label: 'Billed' },
                { key: 'collected', label: 'Collected' },
                { key: 'refunded', label: 'Refunded' },
              ]}
              valueFormatter={(n) => formatMoneyCompact(n, currency)}
              yAxisWidth={72}
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

              <SectionHeading>By payment method</SectionHeading>
              <DataTable columns={methodColumns} data={methodRows} isLoading={isLoading} pageSize={10} />
            </>
          )}

          <p className="font-body text-body-sm text-on-surface-variant mt-6 max-w-3xl">
            Billed counts invoices by issue date. Collected and refunded count payments and refunds by the date
            they were recorded, so they can relate to invoices from an earlier period.
          </p>
        </>
      )}
    </div>
  )
}
