import type { ColumnDef } from '@tanstack/react-table'
import { Link } from 'react-router-dom'
import { CircleDollarSign, FileClock, FileWarning, Receipt } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { InvoiceStatusBadge } from '@/components/billing/InvoiceStatusBadge'
import { BarChart } from '@/components/charts'
import { ChartCard } from '@/components/charts/ChartCard'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { StatTile } from '@/components/ui/stat-tile'
import { useOutstandingReport, type OutstandingInvoiceRow } from '@/api/reports'
import { usePermissions } from '@/hooks/usePermissions'
import { formatCount, formatMoney, formatMoneyCompact } from '@/lib/format'
import { ReportExportButton } from './ReportExportButton'
import { ReportsTabs } from './ReportsTabs'
import { figureColumn } from './reportColumns'
import { KpiGrid, ReportDenied, ReportError, ReportMetaLine, SectionHeading } from './reportParts'
import { AGEING_LABELS, plainDate } from './reportPresentation'
import { REPORT_READ_CODES, holdsAny } from './reportSections'

const EMPTY = 'No unpaid invoices.'

interface AgeRow {
  age: string
  invoices: string
  outstanding: string
}

const ageColumns: ColumnDef<AgeRow>[] = [
  {
    id: 'age',
    header: 'Age',
    enableSorting: false,
    cell: ({ row }) => <span className="font-semibold whitespace-nowrap">{row.original.age}</span>,
  },
  figureColumn<AgeRow>('invoices', 'Invoices'),
  figureColumn<AgeRow>('outstanding', 'Outstanding'),
]

interface InvoiceRow {
  invoiceId: string
  number: string
  /** True when this user may open the invoice itself. */
  canOpen: boolean
  issued: string
  age: string
  patient: string
  mrn: string
  status: OutstandingInvoiceRow['status']
  total: string
  paid: string
  balance: string
}

const invoiceColumns: ColumnDef<InvoiceRow>[] = [
  {
    id: 'invoice',
    header: 'Invoice',
    enableSorting: false,
    cell: ({ row }) =>
      row.original.canOpen ? (
        <Link
          to={`/billing/${row.original.invoiceId}`}
          aria-label={`Invoice ${row.original.number}`}
          className="text-on-surface hover:text-secondary focus-visible:ring-secondary rounded font-mono text-sm font-semibold whitespace-nowrap transition-colors outline-none focus-visible:ring-2"
        >
          {row.original.number}
        </Link>
      ) : (
        <span className="font-mono text-sm font-semibold whitespace-nowrap">{row.original.number}</span>
      ),
  },
  figureColumn<InvoiceRow>('issued', 'Issued'),
  figureColumn<InvoiceRow>('age', 'Age (days)'),
  {
    id: 'patient',
    header: 'Patient',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="block max-w-56 min-w-32 [overflow-wrap:anywhere]">{row.original.patient}</span>
    ),
  },
  {
    id: 'mrn',
    header: 'MRN',
    enableSorting: false,
    cell: ({ row }) => <span className="text-outline font-mono text-xs whitespace-nowrap">{row.original.mrn}</span>,
  },
  {
    id: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => <InvoiceStatusBadge status={row.original.status} />,
  },
  figureColumn<InvoiceRow>('total', 'Total'),
  figureColumn<InvoiceRow>('paid', 'Paid'),
  figureColumn<InvoiceRow>('balance', 'Balance'),
]

/**
 * Unpaid invoices as of now (`GET /reports/outstanding`, which needs
 * `report.admin.read` or `report.billing.read`). A point-in-time report: it
 * has no period and takes no filters.
 *
 * Amounts, ages and the ageing buckets are the server's, counted from the
 * hospital's day. The list is the oldest invoices, as many as the server
 * sends; when there are more, it says how many and the export has them all.
 */
export default function OutstandingReportPage() {
  const { can, canAny } = usePermissions()
  // The route guards this page, but the request is the page's own.
  const canRead = holdsAny(can, REPORT_READ_CODES.outstanding)
  const canOpenInvoices = canAny('invoice.read')
  const { data, isPending, isError, error, refetch } = useOutstandingReport({ enabled: canRead })

  if (!canRead) return <ReportDenied />

  const isLoading = isPending
  const currency = data?.meta.currency
  const summary = data?.summary
  const money = (value: string) => formatMoney(value, currency)
  const isEmpty = summary?.invoice_count === 0

  const ageRows: AgeRow[] = (data?.ageing ?? []).map((bucket) => ({
    age: AGEING_LABELS[bucket.bucket] ?? bucket.bucket,
    invoices: formatCount(bucket.invoice_count),
    outstanding: money(bucket.outstanding_amount),
  }))
  const invoiceRows: InvoiceRow[] = (data?.invoices ?? []).map((invoice) => ({
    invoiceId: invoice.invoice_id,
    number: invoice.invoice_number,
    canOpen: canOpenInvoices,
    issued: plainDate(invoice.issued_date),
    age: formatCount(invoice.age_days),
    patient: invoice.patient_name,
    mrn: invoice.patient_mrn,
    status: invoice.status,
    total: money(invoice.total),
    paid: money(invoice.amount_paid),
    balance: money(invoice.balance_due),
  }))

  return (
    <div className="w-full">
      <ReportsTabs />
      <PageHeader
        title="Outstanding report"
        subtitle="Invoices that are issued and not yet paid in full, as of today."
        actions={<ReportExportButton reportId="outstanding" period={data?.filters} />}
      />

      {isError ? (
        <ReportError error={error} onRetry={() => refetch()} />
      ) : (
        <>
          <ReportMetaLine
            meta={data?.meta}
            withCurrency
            lead={data && `As of ${plainDate(data.filters.as_of_date)}`}
          />
          <KpiGrid>
            <StatTile
              label="Outstanding"
              icon={CircleDollarSign}
              value={summary && money(summary.outstanding_amount)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Unpaid invoices"
              icon={Receipt}
              value={summary && formatCount(summary.invoice_count)}
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Not yet paid"
              icon={FileWarning}
              value={summary && formatCount(summary.issued_count)}
              hint="Issued, nothing received"
              isLoading={isLoading}
              isError={false}
            />
            <StatTile
              label="Part-paid"
              icon={FileClock}
              value={summary && formatCount(summary.partially_paid_count)}
              hint="Some received, a balance due"
              isLoading={isLoading}
              isError={false}
            />
          </KpiGrid>

          <ChartCard
            title="Outstanding by age"
            description="Days since each invoice was issued."
            isLoading={isLoading}
            isError={false}
            onRetry={() => refetch()}
            isEmpty={isEmpty}
            emptyTitle={EMPTY}
            summary={
              data
                ? `Outstanding amount by invoice age: ${data.ageing
                    .map((bucket) => `${AGEING_LABELS[bucket.bucket] ?? bucket.bucket} ${money(bucket.outstanding_amount)}`)
                    .join(', ')}. ${money(data.summary.outstanding_amount)} in total. The table below has every figure.`
                : ''
            }
          >
            <BarChart
              // Decimal strings become numbers only to set the height of a bar.
              data={(data?.ageing ?? []).map((bucket) => ({
                age: AGEING_LABELS[bucket.bucket] ?? bucket.bucket,
                outstanding: Number(bucket.outstanding_amount),
              }))}
              xKey="age"
              series={[{ key: 'outstanding', label: 'Outstanding' }]}
              valueFormatter={(n) => formatMoneyCompact(n, currency)}
              yAxisWidth={72}
            />
          </ChartCard>

          {!isEmpty && (
            <>
              <SectionHeading>By age</SectionHeading>
              <DataTable columns={ageColumns} data={ageRows} isLoading={isLoading} pageSize={10} />

              <SectionHeading>Unpaid invoices</SectionHeading>
              {data?.invoices_truncated && (
                <p className="font-body text-body-sm text-on-surface-variant mb-3" role="note">
                  Showing the {formatCount(data.invoices.length)} oldest of {formatCount(data.invoices_total)}.
                  Export for the full list.
                </p>
              )}
              <DataTable
                columns={invoiceColumns}
                data={invoiceRows}
                isLoading={isLoading}
                pageSize={25}
                emptyState={<EmptyState title={EMPTY} className="py-10" />}
              />
            </>
          )}
        </>
      )}
    </div>
  )
}
