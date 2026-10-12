import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { DataTable } from '@/components/ui/data-table'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { Skeleton } from '@/components/ui/skeleton'
import { InventoryOrderStatusBadge } from '@/components/inventory/InventoryBadges'
import { quantityLabel } from '@/components/inventory/inventoryPresentation'
import {
  useInventoryOrder,
  useLocations,
  type InventoryOrder,
  type InventoryOrderItem,
} from '@/api/inventory'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime, formatMoney } from '@/lib/format'
import { InventoryOrderActions } from './InventoryOrderDialogs'
import { MAX_ORDER_LINES } from './inventoryOrderForm'

/** Every location, active or not: an order may have been received into one since switched off. */
const EVERY_LOCATION = {} as const

/**
 * What the status does and does not mean. Each note is here because the API
 * leaves something out that the word alone would suggest (§10.7): "sent"
 * transmits nothing, "received" reports the location but nothing about what
 * arrived, and "cancelled" carries neither a reason nor a time.
 */
function StatusNote({ order }: { order: InventoryOrder }) {
  const { can } = usePermissions()
  const canManage = can('inventory.po.update')
  const canReceive = can('inventory.po.receive')
  const canOpenStock = can('inventory.stock.read')
  const cannotEdit = canManage
    ? 'An order cannot be edited: to change the vendor, an item, a quantity or a price, cancel it and draft another.'
    : 'An order cannot be edited — someone who manages purchasing has to cancel it and draft another.'

  if (order.status === 'draft') {
    return (
      <Alert variant="info" title="Draft — not sent yet">
        {cannotEdit}
        {canReceive ? ' It can be received once it has been marked as sent.' : ''}
      </Alert>
    )
  }
  if (order.status === 'sent') {
    return (
      <Alert variant="info" title="Marked as sent">
        “Sent” only records the status and the time. Aetheris does not transmit anything to the
        vendor, so the order has to reach them some other way. {cannotEdit}
        {canReceive
          ? ' Receive it when the delivery arrives — an order is received once, in one go.'
          : ''}
      </Alert>
    )
  }
  if (order.status === 'received') {
    return (
      <Alert variant="success" title="Received and closed">
        The lines below still show what was <strong>ordered</strong>. What arrived — the
        quantities, the batches and their expiry dates — is not shown on the order, so a short or
        an over delivery does not appear here.{' '}
        {canOpenStock
          ? "What was added is in each item's stock and in the movement ledger: each line links to both. In the ledger, choose the item to see only its entries."
          : "What was added is in the movement ledger and in each item's stock, which your role cannot open."}
      </Alert>
    )
  }
  return (
    <Alert variant="error" title="This order was cancelled">
      The order keeps no reason and no time for its cancellation, so neither can be shown.
      {order.ordered_at ? ' It had been marked as sent before it was cancelled.' : ''} A cancelled
      order cannot be reopened.
    </Alert>
  )
}

/**
 * Where a received order's goods went. The order carries the location's id
 * only (`received_location_id`), so its name comes from the location list —
 * `GET /inventory/locations`, which needs `inventory.location.read` and is
 * not asked for without it, or for an order that has not been received.
 */
function ReceivedInto({ locationId }: { locationId: string }) {
  const { can } = usePermissions()
  const canReadLocations = can('inventory.location.read')
  const { data, isError } = useLocations(EVERY_LOCATION, { enabled: canReadLocations })
  if (!canReadLocations) {
    return <span className="text-on-surface-variant">Not shown: your role cannot read locations</span>
  }
  const location = data?.find((l) => l.id === locationId)
  if (location) return `${location.name} (${location.code})`
  if (isError) return <span className="text-on-surface-variant">The location's name couldn't be loaded</span>
  if (!data) return <span className="text-on-surface-variant">Loading…</span>
  return <span className="text-on-surface-variant">A location that is no longer listed</span>
}

/**
 * One inventory purchase order (`GET /inventory/purchase-orders/{id}`): who it
 * is with, where it stands and what was ordered. Every quantity, price and
 * total is the server's, and is of what was ordered — nothing here is
 * recomputed. A received order names the location the goods went into; it
 * does not say what was delivered, because the API does not
 * (`InventoryPurchaseOrderItemResponse` has only the ordered quantity).
 */
export default function InventoryOrderDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can } = usePermissions()
  // An item's stock and its ledger are both behind this one permission (§10.1).
  const canOpenStock = can('inventory.stock.read')
  const { data: order, isError, error, refetch } = useInventoryOrder(orderId)

  const columns: ColumnDef<InventoryOrderItem>[] = useMemo(
    () => [
      {
        accessorKey: 'item_name',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex max-w-72 min-w-40 flex-col">
            <span className="text-on-surface font-semibold [overflow-wrap:anywhere]">
              {row.original.item_name}
            </span>
            <span className="text-outline font-mono text-xs [overflow-wrap:anywhere]">
              {row.original.item_sku}
            </span>
          </div>
        ),
      },
      {
        // The order line carries no unit of measure, so none is shown.
        accessorKey: 'quantity',
        header: 'Ordered',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="whitespace-nowrap tabular-nums">{quantityLabel(row.original.quantity)}</span>
        ),
      },
      {
        accessorKey: 'unit_price',
        header: 'Unit price',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="whitespace-nowrap tabular-nums">{formatMoney(row.original.unit_price)}</span>
        ),
      },
      {
        accessorKey: 'total',
        header: 'Line total',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="whitespace-nowrap tabular-nums">{formatMoney(row.original.total)}</span>
        ),
      },
      // Where what actually arrived can be read: the item's stock rows and the
      // movement ledger. Offered only to a user who may open them. The ledger
      // link carries the item's id, but its name promises only the ledger: the
      // ledger page decides whether it opens already narrowed to the item.
      ...(canOpenStock
        ? ([
            {
              id: 'links',
              header: '',
              enableSorting: false,
              cell: ({ row }) => (
                <div className="font-body text-body-sm flex flex-wrap justify-end gap-x-4 gap-y-1">
                  <Link
                    to={`/inventory/stock?item_id=${row.original.item_id}`}
                    aria-label={`Stock of ${row.original.item_name}`}
                    className="text-secondary whitespace-nowrap hover:underline"
                  >
                    Stock
                  </Link>
                  <Link
                    to={`/inventory/movements?item_id=${row.original.item_id}`}
                    aria-label={`Movement ledger, to look up ${row.original.item_name}`}
                    className="text-secondary whitespace-nowrap hover:underline"
                  >
                    Movement ledger
                  </Link>
                </div>
              ),
            },
          ] satisfies ColumnDef<InventoryOrderItem>[])
        : []),
    ],
    [canOpenStock],
  )

  const backLink = (
    <Link
      to="/inventory/purchase-orders"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to purchase orders
    </Link>
  )

  if (isError) {
    const status = error instanceof ApiError ? error.status : undefined
    // An id that is not a UUID is a 422 on `path.order_id`: to the user that
    // is an order that does not exist, not a form to correct.
    const notFound = status === 404 || status === 422
    const denied = status === 403
    return (
      <div className="w-full space-y-4">
        {backLink}
        <Alert
          variant="error"
          title={
            notFound
              ? 'Purchase order not found'
              : denied
                ? "You can't open this purchase order"
                : "Couldn't load this purchase order"
          }
        >
          <div className="flex flex-col items-start gap-3">
            <p>
              {notFound
                ? "This purchase order doesn't exist, or you don't have access to it."
                : denied
                  ? 'Your account is not allowed to read inventory purchase orders. Ask an administrator if you need access.'
                  : 'The purchase order could not be reached. Check your connection and try again.'}
            </p>
            {/* Asking again cannot change a missing order or a missing
                permission, so Retry is for failures that might pass. */}
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

  if (!order) {
    return (
      <div className="w-full space-y-6" aria-busy="true" aria-label="Loading purchase order">
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
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-headline-md text-primary font-mono font-bold [overflow-wrap:anywhere]">
              {order.po_number}
            </h1>
            <InventoryOrderStatusBadge status={order.status} />
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1 [overflow-wrap:anywhere]">
            Inventory purchase order to {order.vendor_name}
          </p>
        </div>
        <InventoryOrderActions order={order} />
      </header>

      <StatusNote order={order} />

      <InfoCard title="Details">
        <Detail label="Vendor" value={order.vendor_name} />
        <Detail label="Created" value={formatDateTime(order.created_at)} />
        <Detail label="Marked as sent" value={order.ordered_at && formatDateTime(order.ordered_at)} />
        <Detail label="Received" value={order.received_at && formatDateTime(order.received_at)} />
        <Detail
          label="Received into"
          value={order.received_location_id && <ReceivedInto locationId={order.received_location_id} />}
        />
        <Detail label="Total ordered" value={formatMoney(order.total_amount)} />
        <Detail label="Notes" value={order.notes} />
      </InfoCard>

      <section className="space-y-3" aria-label="Items ordered">
        <h2 className="font-display text-title-lg text-primary font-bold">Items ordered</h2>
        {/* Every line on one page: an order holds at most fifty. */}
        <DataTable columns={columns} data={order.items} pageSize={MAX_ORDER_LINES} />
        <p className="font-body text-body-sm text-on-surface-variant text-right">
          Total ordered{' '}
          <span className="text-on-surface font-semibold tabular-nums">{formatMoney(order.total_amount)}</span>
        </p>
      </section>
    </div>
  )
}
