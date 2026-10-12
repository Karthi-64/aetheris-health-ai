import { Link } from 'react-router-dom'
import { ArrowRight } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { useInvoices, type InvoiceSummary } from '@/api/billing'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import { InvoiceStatusBadge } from './InvoiceStatusBadge'

/** How many invoices to list before pointing at Billing. */
const SHOWN = 5

/**
 * Invoices a payment can be taken against: issued, or part-paid
 * (docs/18-API_CONTRACTS.md §6.4). For the desk that collects payments —
 * rendered only for a user who may record one. A receptionist's cash-only
 * limit is applied where the payment is taken, not here.
 */
export function InvoicesAwaitingPayment() {
  const { can } = usePermissions()
  const allowed =
    can('invoice.read') && (can('invoice.payment.record') || can('invoice.payment.record.cash'))
  // The list filters by one status at a time, so "can take a payment" is two requests.
  const issued = useInvoices({ status: 'issued', page: 1, page_size: SHOWN }, { enabled: allowed })
  const partPaid = useInvoices(
    { status: 'partially_paid', page: 1, page_size: SHOWN },
    { enabled: allowed },
  )
  if (!allowed) return null

  const isPending = issued.isPending || partPaid.isPending
  const isError = issued.isError || partPaid.isError
  const invoices: InvoiceSummary[] = [...(partPaid.data?.items ?? []), ...(issued.data?.items ?? [])]
  const total = (issued.data?.pagination.total ?? 0) + (partPaid.data?.pagination.total ?? 0)

  return (
    <section className="space-y-4" aria-label="Awaiting payment">
      <div className="flex items-center justify-between">
        <h2 className="font-display text-title-lg text-primary font-bold">Awaiting payment</h2>
        <Link
          to="/billing"
          className="text-secondary font-body text-body-sm inline-flex items-center gap-1 hover:underline"
        >
          Open billing <ArrowRight className="size-4" />
        </Link>
      </div>

      <div className="neo-extruded bg-surface rounded-2xl p-4">
        {isError ? (
          <p className="font-body text-body-sm text-error px-2 py-2">
            Invoices awaiting payment couldn't be loaded.
          </p>
        ) : isPending ? (
          <div role="status" aria-label="Loading invoices awaiting payment" className="space-y-2">
            <Skeleton className="h-10 w-full rounded-xl" />
            <Skeleton className="h-10 w-full rounded-xl" />
          </div>
        ) : invoices.length === 0 ? (
          <p className="font-body text-body-sm text-on-surface-variant px-2 py-2">
            No issued invoices are waiting for payment.
          </p>
        ) : (
          <>
            <ul className="divide-outline-variant/20 divide-y">
              {invoices.slice(0, SHOWN).map((inv) => (
                <li key={inv.id}>
                  <Link
                    to={`/billing/${inv.id}`}
                    className="hover:bg-surface-container-low/60 flex flex-wrap items-center justify-between gap-3 rounded-lg px-2 py-2.5 transition-colors"
                  >
                    <span className="flex flex-wrap items-center gap-2">
                      <span className="font-body text-body-sm text-on-surface font-semibold">
                        {inv.patient_name}
                      </span>
                      <span className="text-primary font-mono text-xs">{inv.invoice_number}</span>
                      <InvoiceStatusBadge status={inv.status} />
                    </span>
                    <span className="font-body text-body-sm text-on-surface tabular-nums">
                      <span className="text-outline">Outstanding </span>
                      <span className="font-semibold">{formatMoney(inv.balance_due, inv.currency)}</span>
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
            {total > SHOWN && (
              <p className="font-body text-outline mt-3 px-2 text-xs">
                Showing {SHOWN} of {total}. Open billing for the rest.
              </p>
            )}
          </>
        )}
      </div>
    </section>
  )
}
