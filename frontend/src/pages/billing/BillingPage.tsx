import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { FilePlus2, Receipt, RotateCw, X } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useInvoices, type InvoiceStatus } from '@/api/billing'
import { usePatient } from '@/api/patients'
import { usePermissions } from '@/hooks/usePermissions'
import { CreateInvoiceDialog } from './CreateInvoiceDialog'
import { invoiceColumns } from './columns'
import { INVOICE_STATUS, INVOICE_STATUSES } from '@/components/billing/invoiceStatus'

const PAGE_SIZE = 25
const ALL = 'all'
/** Not a status: the drafts whose discount is waiting for an admin (§6.7). */
const AWAITING_APPROVAL = 'awaiting_approval'

/**
 * The invoice list (docs/18-API_CONTRACTS.md §6.4), newest first.
 *
 * The API filters by status, by pending discount approval and by patient; it
 * has no text search, so none is offered. `?patient_id=` narrows the list to
 * one patient — that is how a patient's record links here.
 */
export default function BillingPage() {
  const { can } = usePermissions()
  const [searchParams, setSearchParams] = useSearchParams()
  const patientId = searchParams.get('patient_id') ?? undefined
  const [filter, setFilter] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  const { data, isPending, isError, refetch } = useInvoices({
    patient_id: patientId,
    status: filter === ALL || filter === AWAITING_APPROVAL ? undefined : (filter as InvoiceStatus),
    discount_pending: filter === AWAITING_APPROVAL ? true : undefined,
    page,
    page_size: PAGE_SIZE,
  })

  const invoices = data?.items ?? []
  const meta = data?.pagination
  const filtered = filter !== ALL
  // Named from the patient record where the user may read it; otherwise from
  // the rows, which are all this patient's.
  const { data: patient } = usePatient(patientId && can('patient.read') ? patientId : '')
  const patientName = patientId ? (patient?.full_name ?? invoices[0]?.patient_name) : undefined
  // Raising an invoice from a patient's list starts with that patient chosen.
  const createFor = patientId && patient ? patient : undefined

  const newInvoiceButton = (
    <Button className="rounded-full">
      <FilePlus2 className="size-4" /> New invoice
    </Button>
  )

  return (
    <div className="w-full">
      <PageHeader
        title="Billing"
        subtitle="Invoices, payments and outstanding balances."
        actions={
          <>
            <Select
              value={filter}
              onValueChange={(v) => {
                setFilter(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter invoices" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All invoices</SelectItem>
                {INVOICE_STATUSES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {INVOICE_STATUS[s].label}
                  </SelectItem>
                ))}
                <SelectItem value={AWAITING_APPROVAL}>Awaiting approval</SelectItem>
              </SelectContent>
            </Select>
            {can('invoice.create') && <CreateInvoiceDialog key={createFor?.id ?? 'any'} trigger={newInvoiceButton} patient={createFor} />}
          </>
        }
      />

      {patientId && (
        <div className="neo-pressed bg-surface mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-2.5">
          <p className="font-body text-body-sm text-on-surface">
            Showing invoices for{' '}
            {can('patient.read') ? (
              <Link to={`/patients/${patientId}`} className="text-secondary font-semibold hover:underline">
                {patientName ?? 'this patient'}
              </Link>
            ) : (
              <span className="font-semibold">{patientName ?? 'this patient'}</span>
            )}
          </p>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => {
              setSearchParams({})
              setPage(1)
            }}
          >
            <X className="size-4" /> Show all patients
          </Button>
        </div>
      )}

      {isError ? (
        <Alert variant="error" title="Couldn't load invoices">
          <div className="flex flex-col items-start gap-3">
            <p>The invoice list could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={invoiceColumns}
          data={invoices}
          isLoading={isPending}
          pageSize={PAGE_SIZE}
          emptyState={
            filtered || patientId ? (
              <EmptyState
                icon={Receipt}
                title="No matching invoices"
                description={
                  filtered
                    ? 'No invoices match this filter.'
                    : 'This patient has not been invoiced yet.'
                }
              />
            ) : (
              <EmptyState
                icon={Receipt}
                title="No invoices yet"
                description="Invoices are drafted when a consultation is completed, or can be raised by hand."
                action={can('invoice.create') ? <CreateInvoiceDialog key={createFor?.id ?? 'any'} trigger={newInvoiceButton} patient={createFor} /> : undefined}
              />
            )
          }
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
        />
      )}
    </div>
  )
}
