import { Link } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { Skeleton } from '@/components/ui/skeleton'
import { useInvoices } from '@/api/billing'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDate, formatMoney } from '@/lib/format'
import { AwaitingApprovalBadge, InvoiceStatusBadge } from './InvoiceStatusBadge'

/** How many of the newest invoices to show before linking to the full list. */
const RECENT = 5

/**
 * A patient's most recent invoices, linking into Billing. Renders nothing for
 * a user who cannot read invoices. A doctor (`invoice.read.own`) sees only the
 * invoices for their own visits — the server narrows the list.
 */
export function PatientInvoices({ patientId }: { patientId: string }) {
  const { canAny } = usePermissions()
  const allowed = canAny('invoice.read')
  const { data, isPending, isError } = useInvoices(
    { patient_id: patientId, page_size: RECENT },
    { enabled: allowed },
  )
  if (!allowed) return null

  const invoices = data?.items ?? []
  const total = data?.pagination.total ?? 0

  return (
    <section className="neo-extruded bg-surface rounded-2xl p-6" aria-label="Billing">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 className="font-display text-title-lg text-primary font-bold">Billing</h2>
        {total > 0 && (
          <Link
            to={`/billing?patient_id=${patientId}`}
            className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
          >
            {total > RECENT ? `View all ${total} invoices` : 'Open in Billing'}
            <ChevronRight className="size-4" />
          </Link>
        )}
      </div>

      {isError ? (
        <p className="font-body text-body-sm text-error">This patient's invoices couldn't be loaded.</p>
      ) : isPending ? (
        <div className="space-y-2" aria-busy="true" aria-label="Loading invoices">
          <Skeleton className="h-10 w-full rounded-xl" />
          <Skeleton className="h-10 w-full rounded-xl" />
        </div>
      ) : invoices.length === 0 ? (
        <p className="font-body text-body-sm text-on-surface-variant">No invoices for this patient yet.</p>
      ) : (
        <ul className="divide-outline-variant/20 divide-y">
          {invoices.map((inv) => (
            <li key={inv.id}>
              <Link
                to={`/billing/${inv.id}`}
                className="hover:bg-surface-container-low/60 flex flex-wrap items-center justify-between gap-3 rounded-lg px-2 py-2.5 transition-colors"
              >
                <span className="flex flex-wrap items-center gap-2">
                  <span className="font-mono text-primary text-sm">
                    {inv.invoice_number ?? 'Draft invoice'}
                  </span>
                  <span className="font-body text-outline text-xs tabular-nums">
                    {formatDate(inv.issued_at ?? inv.created_at)}
                  </span>
                  <InvoiceStatusBadge status={inv.status} />
                  {inv.discount_pending_approval && <AwaitingApprovalBadge />}
                </span>
                <span className="font-body text-body-sm text-on-surface tabular-nums">
                  {formatMoney(inv.total, inv.currency)}
                  <span className="text-outline"> · outstanding </span>
                  <span className="font-semibold">{formatMoney(inv.balance_due, inv.currency)}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
