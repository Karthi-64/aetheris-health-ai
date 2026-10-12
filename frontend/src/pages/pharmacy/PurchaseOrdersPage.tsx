import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight, ClipboardList, FilePlus2, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { PurchaseOrderStatusBadge } from '@/components/pharmacy/PharmacyBadges'
import { PURCHASE_ORDER_STATUS, PURCHASE_ORDER_STATUSES } from '@/components/pharmacy/pharmacyPresentation'
import { usePurchaseOrders, type PurchaseOrder, type PurchaseOrderStatus } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDate, formatMoney } from '@/lib/format'
import { PharmacyTabs } from './PharmacyTabs'
import { CreatePurchaseOrderDialog, VendorFilter } from './PurchaseOrderDialogs'

const PAGE_SIZE = 25
const ALL = 'all'
/** How many medicine names a row spells out before counting the rest. */
const NAMED = 2

/** "3 lines" with the first medicines named; the full list is the row's title. */
function LinesCell({ order }: { order: PurchaseOrder }) {
  const names = order.items.map((i) => i.medicine_name)
  const rest = names.length - NAMED
  return (
    <div className="flex max-w-64 min-w-36 flex-col" title={names.join(', ')}>
      <span className="text-on-surface tabular-nums">
        {names.length} {names.length === 1 ? 'line' : 'lines'}
      </span>
      <span className="text-outline text-xs [overflow-wrap:anywhere]">
        {names.slice(0, NAMED).join(', ')}
        {rest > 0 ? ` +${rest} more` : ''}
      </span>
    </div>
  )
}

/** List columns. Sorting is off: the API returns newest first and has no sort parameter. */
const columns: ColumnDef<PurchaseOrder>[] = [
  {
    accessorKey: 'po_number',
    header: 'Order',
    enableSorting: false,
    cell: ({ row }) => (
      <Link
        to={`/pharmacy/purchase-orders/${row.original.id}`}
        className="text-on-surface hover:text-secondary font-mono text-sm font-semibold whitespace-nowrap hover:underline"
      >
        {row.original.po_number}
      </Link>
    ),
  },
  {
    accessorKey: 'vendor_name',
    header: 'Vendor',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant block max-w-64 min-w-32 [overflow-wrap:anywhere]">
        {row.original.vendor_name}
      </span>
    ),
  },
  {
    id: 'lines',
    header: 'Lines',
    enableSorting: false,
    cell: ({ row }) => <LinesCell order={row.original} />,
  },
  {
    // The server's sum of what was ORDERED; it does not change on receipt.
    accessorKey: 'total_amount',
    header: 'Total ordered',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="whitespace-nowrap tabular-nums">{formatMoney(row.original.total_amount)}</span>
    ),
  },
  {
    accessorKey: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => <PurchaseOrderStatusBadge status={row.original.status} />,
  },
  {
    accessorKey: 'created_at',
    header: 'Created',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant whitespace-nowrap tabular-nums">
        {formatDate(row.original.created_at)}
      </span>
    ),
  },
  {
    id: 'open',
    header: '',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex justify-end">
        <Link
          to={`/pharmacy/purchase-orders/${row.original.id}`}
          aria-label={`Open purchase order ${row.original.po_number}`}
          className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 transition-colors"
        >
          Open <ChevronRight className="size-4" />
        </Link>
      </div>
    ),
  },
]

/**
 * Purchase orders for medicines (docs/18-API_CONTRACTS.md §9.8), newest first.
 *
 * The API filters by status and by vendor; it has no text search, no date
 * range and no sort, so none is offered. An order is opened to send, receive
 * or cancel it — it cannot be edited, here or anywhere.
 */
export default function PurchaseOrdersPage() {
  const { can } = usePermissions()
  const canCreate = can('pharmacy.po.create')
  const canReadVendors = can('pharmacy.vendor.read')
  const [status, setStatus] = useState<string>(ALL)
  const [vendorId, setVendorId] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  const { data, isPending, isError, refetch } = usePurchaseOrders({
    // "All" leaves the parameter out: sent empty it is a 422, not "no filter".
    status: status === ALL ? undefined : (status as PurchaseOrderStatus),
    vendor_id: vendorId === ALL ? undefined : vendorId,
    page,
    page_size: PAGE_SIZE,
  })

  const orders = data?.items ?? []
  const meta = data?.pagination
  const filtered = status !== ALL || vendorId !== ALL

  const newOrderButton = (
    <Button className="rounded-full">
      <FilePlus2 className="size-4" /> New order
    </Button>
  )

  return (
    <div className="w-full">
      <PharmacyTabs />
      <PageHeader
        title="Purchase orders"
        subtitle="Medicines ordered from vendors, from draft to delivery."
        actions={
          <>
            <Select
              value={status}
              onValueChange={(v) => {
                setStatus(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by status" className="w-40 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All statuses</SelectItem>
                {PURCHASE_ORDER_STATUSES.map((s) => (
                  <SelectItem key={s} value={s}>
                    {PURCHASE_ORDER_STATUS[s].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* The vendor list is its own permission, and the filter is what
                asks for it: it is mounted only for a user who holds it. */}
            {canReadVendors && (
              <VendorFilter
                value={vendorId}
                all={ALL}
                onChange={(v) => {
                  setVendorId(v)
                  setPage(1)
                }}
              />
            )}
            {canCreate && <CreatePurchaseOrderDialog trigger={newOrderButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load purchase orders">
          <div className="flex flex-col items-start gap-3">
            <p>The purchase order list could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={orders}
          isLoading={isPending}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState
                icon={ClipboardList}
                title="No matching purchase orders"
                description="No purchase orders match these filters."
              />
            ) : (
              <EmptyState
                icon={ClipboardList}
                title="No purchase orders yet"
                description={
                  canCreate
                    ? 'Draft an order for a vendor, mark it as sent once the vendor has it, and receive it when the delivery arrives — receiving is what adds the medicines to stock.'
                    : 'An order drafted by someone who manages purchasing appears here. Once it is marked as sent, it can be received when the delivery arrives.'
                }
                action={canCreate ? <CreatePurchaseOrderDialog trigger={newOrderButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
