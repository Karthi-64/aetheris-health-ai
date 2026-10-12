import { createContext, useContext, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, Boxes, PackagePlus, RotateCw, ShieldAlert, ShieldCheck, SlidersHorizontal } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import { useMedicineStock, type Medicine, type MedicineBatch } from '@/api/pharmacy'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatMoney } from '@/lib/format'
import { cn } from '@/lib/utils'
import { BatchStateBadge } from '@/components/pharmacy/PharmacyBadges'
import { batchState, medicineDetail, units } from '@/components/pharmacy/pharmacyPresentation'
import { AdjustStockDialog, LiftRecallDialog, ReceiveBatchDialog, RecallBatchDialog } from './StockDialogs'
import { expiryDateLabel, expiryInWords } from './stockForm'

/** Every batch on one page in practice; the list is not paged by the API. */
const BATCHES_PER_PAGE = 50

/**
 * The medicine whose batches are listed, for the cells of the table below.
 * The columns are constants so that a cell — and a dialog open inside it —
 * stays mounted while the stock is refetched behind it; the medicine therefore
 * reaches the cells this way rather than through the column definitions.
 */
const StockMedicine = createContext<Medicine | null>(null)

/** What can be done to one batch. Both writes need `pharmacy.batch.update` (§9.1). */
function BatchActions({ batch }: { batch: MedicineBatch }) {
  const medicine = useContext(StockMedicine)
  if (!medicine) return null

  return (
    <div className="flex items-center justify-end gap-1">
      <AdjustStockDialog
        medicine={medicine}
        batch={batch}
        trigger={
          <Button variant="ghost" size="sm" aria-label={`Adjust batch ${batch.batch_number}`}>
            <SlidersHorizontal className="size-4" /> Adjust
          </Button>
        }
      />
      {batch.is_recalled ? (
        <LiftRecallDialog
          medicine={medicine}
          batch={batch}
          trigger={
            <Button variant="ghost" size="sm" aria-label={`Lift recall on batch ${batch.batch_number}`}>
              <ShieldCheck className="size-4" /> Lift recall
            </Button>
          }
        />
      ) : (
        <RecallBatchDialog
          medicine={medicine}
          batch={batch}
          trigger={
            <Button
              variant="ghost"
              size="sm"
              className="text-error hover:text-error"
              aria-label={`Recall batch ${batch.batch_number}`}
            >
              <ShieldAlert className="size-4" /> Recall
            </Button>
          }
        />
      )}
    </div>
  )
}

/**
 * A batch's state. The server's flags do not look at the medicine: for an
 * inactive one they still call an in-date, unrecalled batch dispensable, and
 * no dispense of an inactive medicine is accepted (§9.3). Such a batch is
 * shown as held, not as dispensable; recalled, expired and empty are true
 * either way and stay as they are.
 */
function BatchStateCell({ batch }: { batch: MedicineBatch }) {
  const medicine = useContext(StockMedicine)
  const state = batchState(batch)
  if (medicine && !medicine.is_active && (state === 'ok' || state === 'expiring')) {
    return <Badge variant="neutral">Held — medicine inactive</Badge>
  }
  return <BatchStateBadge batch={batch} />
}

/**
 * Batch columns. Sorting is off: the API returns the earliest expiry first and
 * that order is kept. Every date-dependent value — the days to expiry and the
 * state — is the server's, worked out on the hospital's own date (§9.3).
 */
const batchColumns: ColumnDef<MedicineBatch>[] = [
  {
    accessorKey: 'batch_number',
    header: 'Batch',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface block max-w-48 min-w-24 font-mono font-semibold [overflow-wrap:anywhere]">
        {row.original.batch_number}
      </span>
    ),
  },
  {
    accessorKey: 'expiry_date',
    header: 'Expiry',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex flex-col whitespace-nowrap">
        <span className="text-on-surface tabular-nums">{expiryDateLabel(row.original.expiry_date)}</span>
        <span className="text-outline text-xs">{expiryInWords(row.original.days_to_expiry)}</span>
      </div>
    ),
  },
  {
    accessorKey: 'quantity_on_hand',
    header: 'On hand',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-semibold tabular-nums">{row.original.quantity_on_hand.toLocaleString()}</span>
    ),
  },
  {
    // Every unit ever received into the batch. It is not a ceiling for the
    // count on hand, which an upward correction can take past it.
    accessorKey: 'initial_quantity',
    header: 'Received in total',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant tabular-nums">{row.original.initial_quantity.toLocaleString()}</span>
    ),
  },
  {
    accessorKey: 'cost_per_unit',
    header: 'Cost per unit',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant tabular-nums">{formatMoney(row.original.cost_per_unit)}</span>
    ),
  },
  {
    id: 'state',
    header: 'State',
    enableSorting: false,
    cell: ({ row }) => <BatchStateCell batch={row.original} />,
  },
]

const batchColumnsWithActions: ColumnDef<MedicineBatch>[] = [
  ...batchColumns,
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    // The table keeps a cell by its row's position, not by its batch. Keyed by
    // the batch, an open dialog is closed if a refetch puts another batch in
    // this row — it can never carry on against a batch it was not opened for.
    cell: ({ row }) => <BatchActions key={row.original.id} batch={row.original} />,
  },
]

/** One of the server's stock counts, with what it does and does not include. */
function Figure({
  label,
  value,
  note,
  muted,
  children,
}: {
  label: string
  value: number
  note: string
  muted?: boolean
  children?: ReactNode
}) {
  return (
    <div className="neo-extruded bg-surface min-w-0 rounded-2xl p-5">
      <dt className="font-label text-label-caps text-on-surface-variant">{label}</dt>
      <dd className="mt-2 space-y-1.5">
        <p
          className={cn(
            'font-display text-headline-md font-bold tabular-nums',
            muted ? 'text-on-surface-variant' : 'text-primary',
          )}
        >
          {units(value)}
        </p>
        {children}
        <p className="font-body text-outline text-xs">{note}</p>
      </dd>
    </div>
  )
}

/**
 * One medicine's stock (`GET /medicines/{id}/stock`, which needs
 * `pharmacy.batch.read`): the server's totals and every batch it holds.
 *
 * The five totals are shown as the server gives them and are never added up,
 * because they overlap — on hand includes expired and recalled units, a batch
 * both expired and recalled is counted under each, and the units expiring soon
 * are among the dispensable ones (§9.3).
 *
 * The server's dispensable count ignores whether the medicine is active, and
 * an inactive medicine cannot be dispensed at all, so for one that count is
 * not presented as available stock.
 */
export default function MedicineStockPage() {
  const { medicineId = '' } = useParams<{ medicineId: string }>()
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own: it is not
  // made for someone the endpoint would refuse, whatever the page is mounted under.
  const canRead = can('pharmacy.batch.read')
  const { data: stock, isError, error, refetch, isFetching } = useMedicineStock(medicineId, { enabled: canRead })

  const backLink = (
    <Link
      to="/pharmacy/medicines"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to medicines
    </Link>
  )

  const status = isError && error instanceof ApiError ? error.status : undefined
  // A 422 here is an id that is not a UUID — an address no medicine can have.
  const notFound = status === 404 || status === 422
  // The permission list is a snapshot taken at sign-in; the server checks the live one.
  const denied = !canRead || status === 403
  // A failed re-read does not take away a page that has already loaded: a
  // dialog open on it, with what was typed and the server's refusal in it,
  // would go too. The page gives way only when there is nothing to show, or
  // when the server says the medicine is gone or no longer this user's to see.
  const outOfDate = isError && !!stock && !notFound && !denied

  if (denied || (isError && !outOfDate)) {
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert
          variant="error"
          title={
            notFound
              ? 'Medicine not found'
              : denied
                ? "You can't view stock"
                : "Couldn't load this medicine's stock"
          }
        >
          <div className="flex flex-col items-start gap-3">
            <p>
              {notFound
                ? "This medicine doesn't exist, or you don't have access to it."
                : denied
                  ? 'Your account is not permitted to see stock levels. If that has just changed, sign in again.'
                  : 'The stock could not be reached. Check your connection and try again.'}
            </p>
            {!notFound && !denied && (
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RotateCw className="size-4" /> Retry
              </Button>
            )}
          </div>
        </Alert>
      </div>
    )
  }

  if (!stock) {
    return (
      <div className="w-full space-y-6" role="status" aria-busy="true" aria-label="Loading stock">
        {backLink}
        <Skeleton className="h-24 w-full rounded-2xl" />
        <Skeleton className="h-32 w-full rounded-2xl" />
        <Skeleton className="h-48 w-full rounded-2xl" />
      </div>
    )
  }

  const { medicine } = stock
  const detail = medicineDetail(medicine)
  const canReceive = can('pharmacy.batch.create')
  const canUpdateBatch = can('pharmacy.batch.update')

  return (
    <div className="w-full space-y-6">
      {backLink}

      <header className="glassmorphism shadow-glass-panel flex flex-wrap items-center justify-between gap-4 rounded-2xl p-6">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="font-display text-headline-md text-primary font-bold [overflow-wrap:anywhere]">
              {medicine.name}
            </h1>
            {medicine.is_active ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Inactive</Badge>}
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1 [overflow-wrap:anywhere]">
            <span className="text-outline font-mono text-sm">{medicine.sku}</span>
            {detail && <> · {detail}</>}
            {' · '}
            Selling price <span className="tabular-nums">{formatMoney(medicine.unit_price)}</span> per unit
          </p>
        </div>
        {/* The API refuses a direct receipt for an inactive medicine (400), so it is not offered. */}
        {canReceive && medicine.is_active && (
          <ReceiveBatchDialog
            key={medicine.id}
            medicine={medicine}
            trigger={
              <Button>
                <PackagePlus className="size-4" /> Receive stock
              </Button>
            }
          />
        )}
      </header>

      {outOfDate && (
        <Alert variant="warning" title="These figures may be out of date">
          <div className="flex flex-col items-start gap-3">
            <p>The stock could not be read again just now. What is shown is the last reading.</p>
            <Button variant="outline" size="sm" disabled={isFetching} onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      )}

      {!medicine.is_active && (
        <Alert variant="warning" title="This medicine is inactive">
          It cannot be prescribed, ordered or dispensed, and no stock can be received for it
          {canReceive ? ' — which is why there is no Receive stock button here' : ''}.
          {canUpdateBatch ? ' The batches already held can still be adjusted or recalled.' : ''} It is
          made active again by editing it in the catalog.
        </Alert>
      )}

      <section className="space-y-3" aria-label="Stock summary">
        <h2 className="font-display text-title-lg text-primary font-bold">Stock</h2>
        <dl className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
          {medicine.is_active ? (
            <Figure
              label="Dispensable today"
              value={stock.dispensable_quantity}
              note="In stock, not expired and not recalled."
            />
          ) : (
            <Figure
              label="In date, not recalled"
              value={stock.dispensable_quantity}
              muted
              note="Units that are neither expired nor recalled. None of them can be dispensed while the medicine is inactive."
            >
              <p className="font-body text-body-sm text-on-surface font-semibold">Inactive — cannot be dispensed</p>
            </Figure>
          )}
          <Figure
            label="On hand"
            value={stock.quantity_on_hand}
            note="Every unit held, including expired and recalled."
          />
          <Figure
            label="Expiring within 30 days"
            value={stock.expiring_soon_quantity}
            note={
              medicine.is_active
                ? 'Dispensable units whose batch expires within 30 days. Already counted as dispensable.'
                : 'In-date, unrecalled units whose batch expires within 30 days.'
            }
          />
          <Figure
            label="Expired"
            value={stock.expired_quantity}
            note="Still held, past their expiry date. Cannot be dispensed."
          />
          <Figure
            label="Recalled"
            value={stock.recalled_quantity}
            note="Still held, in a recalled batch. Cannot be dispensed."
          />
        </dl>
        <p className="font-body text-outline text-xs">
          These are separate counts, not parts of one total: a batch that is both expired and
          recalled is counted under each, and on hand includes them all.
        </p>
      </section>

      <section className="space-y-3" aria-label="Batches">
        <div>
          <h2 className="font-display text-title-lg text-primary font-bold">Batches</h2>
          <p className="font-body text-body-sm text-on-surface-variant">
            Earliest expiry first. A batch's number, expiry date and cost are fixed when it is first received.
          </p>
        </div>
        <StockMedicine.Provider value={medicine}>
          <DataTable
            columns={canUpdateBatch ? batchColumnsWithActions : batchColumns}
            data={stock.batches}
            pageSize={BATCHES_PER_PAGE}
            emptyState={
              <EmptyState
                icon={Boxes}
                title="No batches yet"
                description={
                  canReceive && medicine.is_active
                    ? 'No stock has been received for this medicine. Use Receive stock to record its first batch.'
                    : 'No stock has been received for this medicine. Batches appear here when it is.'
                }
              />
            }
          />
        </StockMedicine.Provider>
      </section>
    </div>
  )
}
