import { type ReactNode, useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { DataTable } from '@/components/ui/data-table'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { useAppointment } from '@/api/appointments'
import { useInvoice, useInvoicePayments, useInvoiceRefunds, type Invoice } from '@/api/billing'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDate, formatMoney, formatTime } from '@/lib/format'
import { InvoiceActions } from './InvoiceActions'
import { AwaitingApprovalBadge, InvoiceStatusBadge } from '@/components/billing/InvoiceStatusBadge'
import { invoiceItemColumns, paymentColumns, refundColumns } from './columns'

const dateTime = (iso: string) => `${formatDate(iso)}, ${formatTime(iso)}`

/** The visit an invoice bills, for users who may read appointments. */
function AppointmentDetail({ appointmentId }: { appointmentId: string }) {
  const { can } = usePermissions()
  const { data: appointment } = useAppointment(appointmentId, { enabled: can('appointment.read') })
  if (!appointment) return <>Linked to an appointment</>
  return (
    <>
      {dateTime(appointment.scheduled_start)} · {appointment.doctor_name}
    </>
  )
}

/** One row of the totals block. `value` is always a figure the server sent. */
function TotalRow({
  label,
  value,
  currency,
  strong,
  note,
}: {
  label: string
  value: string
  currency: string
  strong?: boolean
  note?: string
}) {
  return (
    <div className="flex items-baseline justify-between gap-6">
      <dt className="font-body text-body-sm text-on-surface-variant">
        {label}
        {note && <span className="text-outline block text-xs">{note}</span>}
      </dt>
      <dd
        className={
          strong
            ? 'font-display text-title-lg text-primary font-bold tabular-nums'
            : 'font-body text-body-md text-on-surface tabular-nums'
        }
      >
        {formatMoney(value, currency)}
      </dd>
    </div>
  )
}

/**
 * The totals exactly as the server holds them (docs/18-API_CONTRACTS.md §6.2).
 * Nothing here is added up in the browser.
 */
function Totals({ invoice }: { invoice: Invoice }) {
  const { currency } = invoice
  const hasDiscount = /[1-9]/.test(invoice.discount_amount)
  const hasRefund = /[1-9]/.test(invoice.amount_refunded)
  return (
    <section className="neo-extruded bg-surface rounded-2xl p-6" aria-label="Totals">
      <h2 className="font-display text-title-lg text-primary mb-4 font-bold">Totals</h2>
      <dl className="ml-auto max-w-sm space-y-2">
        <TotalRow label="Subtotal" value={invoice.subtotal} currency={currency} />
        <TotalRow label="Tax" value={invoice.tax_amount} currency={currency} />
        {hasDiscount && (
          <TotalRow
            label="Discount"
            value={invoice.discount_amount}
            currency={currency}
            note={invoice.discount_reason ?? undefined}
          />
        )}
        <div className="border-outline-variant/40 border-t pt-2">
          <TotalRow label="Total" value={invoice.total} currency={currency} strong />
        </div>
        <TotalRow label="Paid" value={invoice.amount_paid} currency={currency} />
        {hasRefund && <TotalRow label="Refunded" value={invoice.amount_refunded} currency={currency} />}
        <div className="border-outline-variant/40 border-t pt-2">
          <TotalRow label="Outstanding" value={invoice.balance_due} currency={currency} strong />
        </div>
      </dl>
    </section>
  )
}

function HistoryCard({ title, empty, children }: { title: string; empty?: string; children?: ReactNode }) {
  return (
    <section className="space-y-3" aria-label={title}>
      <h2 className="font-display text-title-lg text-primary font-bold">{title}</h2>
      {empty ? (
        <p className="neo-pressed bg-surface font-body text-body-sm text-on-surface-variant rounded-xl px-4 py-3">
          {empty}
        </p>
      ) : (
        children
      )}
    </section>
  )
}

export default function InvoiceDetailPage() {
  const { invoiceId = '' } = useParams<{ invoiceId: string }>()
  const { can } = usePermissions()
  const { data: invoice, isError, error, refetch } = useInvoice(invoiceId)
  // A draft has taken no money, so there is no history to ask for.
  const hasHistory = !!invoice && invoice.status !== 'draft'
  const payments = useInvoicePayments(invoiceId, { enabled: hasHistory })
  const refunds = useInvoiceRefunds(invoiceId, { enabled: hasHistory })

  const currency = invoice?.currency ?? ''
  const itemCols = useMemo(() => invoiceItemColumns(currency), [currency])
  const paymentCols = useMemo(() => paymentColumns(currency), [currency])
  const refundCols = useMemo(() => refundColumns(currency), [currency])

  const backLink = (
    <Link
      to="/billing"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to billing
    </Link>
  )

  if (isError) {
    // 404 is also what a doctor gets for an invoice that is not from their own visits.
    const notFound = error instanceof ApiError && error.status === 404
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert variant="error" title={notFound ? 'Invoice not found' : "Couldn't load this invoice"}>
          <div className="flex flex-col items-start gap-3">
            <p>
              {notFound
                ? "This invoice doesn't exist, or you don't have access to it."
                : 'The invoice could not be reached. Check your connection and try again.'}
            </p>
            {!notFound && (
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RotateCw className="size-4" /> Retry
              </Button>
            )}
          </div>
        </Alert>
      </div>
    )
  }

  if (!invoice) {
    return (
      <div className="w-full space-y-6" aria-busy="true" aria-label="Loading invoice">
        {backLink}
        <Skeleton className="h-24 w-full rounded-2xl" />
        <Skeleton className="h-40 w-full rounded-2xl" />
        <Skeleton className="h-48 w-full rounded-2xl" />
      </div>
    )
  }

  return (
    <div className="w-full space-y-6">
      {backLink}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div>
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-display text-headline-md text-primary font-bold">
              {invoice.invoice_number ?? 'Draft invoice'}
            </h1>
            <InvoiceStatusBadge status={invoice.status} />
            {invoice.discount_pending_approval && <AwaitingApprovalBadge />}
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1">
            {can('patient.read') ? (
              <Link to={`/patients/${invoice.patient_id}`} className="text-secondary hover:underline">
                {invoice.patient_name}
              </Link>
            ) : (
              invoice.patient_name
            )}
          </p>
        </div>
        <InvoiceActions invoice={invoice} />
      </header>

      {invoice.discount_pending_approval && (
        <Alert variant="warning" title="Discount awaiting approval">
          This draft can't be issued until an administrator approves the discount of{' '}
          {formatMoney(invoice.discount_amount, invoice.currency)}.
        </Alert>
      )}
      {invoice.status === 'void' && (
        <Alert variant="error" title="This invoice was voided">
          {invoice.void_reason}
        </Alert>
      )}

      <InfoCard title="Details">
        <Detail label="Patient" value={invoice.patient_name} />
        <Detail
          label="Appointment"
          value={
            invoice.appointment_id ? (
              <AppointmentDetail appointmentId={invoice.appointment_id} />
            ) : (
              'Not linked to an appointment'
            )
          }
        />
        <Detail label="Created" value={dateTime(invoice.created_at)} />
        <Detail label="Issued" value={invoice.issued_at ? dateTime(invoice.issued_at) : null} />
        {invoice.voided_at && <Detail label="Voided" value={dateTime(invoice.voided_at)} />}
        <Detail label="Notes" value={invoice.notes} />
      </InfoCard>

      <HistoryCard
        title="Charges"
        empty={invoice.items.length === 0 ? 'No charges have been added to this invoice yet.' : undefined}
      >
        <DataTable columns={itemCols} data={invoice.items} pageSize={200} />
      </HistoryCard>

      <Totals invoice={invoice} />

      {hasHistory && (
        <HistoryCard
          title="Payments"
          empty={
            payments.isError
              ? "The payment history couldn't be loaded."
              : payments.data?.length === 0
                ? 'No payments have been recorded.'
                : undefined
          }
        >
          <DataTable columns={paymentCols} data={payments.data ?? []} isLoading={payments.isPending} pageSize={200} />
        </HistoryCard>
      )}

      {hasHistory && (refunds.isError || (refunds.data?.length ?? 0) > 0) && (
        <HistoryCard
          title="Refunds"
          empty={refunds.isError ? "The refund history couldn't be loaded." : undefined}
        >
          <DataTable columns={refundCols} data={refunds.data ?? []} pageSize={200} />
        </HistoryCard>
      )}
    </div>
  )
}
