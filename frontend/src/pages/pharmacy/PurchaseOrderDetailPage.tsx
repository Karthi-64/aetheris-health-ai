import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeft, RotateCw } from 'lucide-react'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { DataTable } from '@/components/ui/data-table'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import { Skeleton } from '@/components/ui/skeleton'
import { PurchaseOrderStatusBadge } from '@/components/pharmacy/PharmacyBadges'
import { units } from '@/components/pharmacy/pharmacyPresentation'
import { usePurchaseOrder, type PurchaseOrder, type PurchaseOrderItem } from '@/api/pharmacy'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime, formatMoney } from '@/lib/format'
import { PurchaseOrderActions } from './PurchaseOrderDialogs'
import { MAX_ORDER_LINES } from './purchaseOrderForm'

/**
 * What the status does and does not mean. Each note is here because the API
 * leaves something out that the word alone would suggest (§9.8): "sent"
 * transmits nothing, "received" reports nothing about what arrived, and
 * "cancelled" carries neither a reason nor a time.
 */
function StatusNote({ order }: { order: PurchaseOrder }) {
  const { can } = usePermissions()
  const canManage = can('pharmacy.po.update')
  const canReceive = can('pharmacy.po.receive')
  const canOpenStock = can('pharmacy.batch.read')
  const cannotEdit = canManage
    ? 'An order cannot be edited: to change the vendor, a medicine, a quantity or a price, cancel it and draft another.'
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
        The lines below still show what was <strong>ordered</strong>. What arrived — the batches,
        their quantities and costs — is not shown on the order, so a short or an over delivery does
        not appear here.{' '}
        {canOpenStock
          ? "To see what was added, open each medicine's stock."
          : "What was added shows in each medicine's stock, which your role cannot open."}
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
 * One purchase order (`GET /purchase-orders/{id}`): who it is with, where it
 * stands and what was ordered. Every quantity, price and total is the
 * server's, and is of what was ordered — nothing here is recomputed, and
 * nothing here says what was delivered, because the API does not.
 */
export default function PurchaseOrderDetailPage() {
  const { orderId = '' } = useParams<{ orderId: string }>()
  const { can } = usePermissions()
  // A medicine's page is its stock, which is its own permission.
  const canOpenStock = can('pharmacy.batch.read')
  const { data: order, isError, error, refetch } = usePurchaseOrder(orderId)

  const columns: ColumnDef<PurchaseOrderItem>[] = useMemo(
    () => [
      {
        accessorKey: 'medicine_name',
        header: 'Medicine',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex max-w-72 min-w-40 flex-col">
            {canOpenStock ? (
              <Link
                to={`/pharmacy/medicines/${row.original.medicine_id}`}
                className="text-on-surface hover:text-secondary font-semibold [overflow-wrap:anywhere] hover:underline"
              >
                {row.original.medicine_name}
              </Link>
            ) : (
              <span className="text-on-surface font-semibold [overflow-wrap:anywhere]">
                {row.original.medicine_name}
              </span>
            )}
            <span className="text-outline font-mono text-xs [overflow-wrap:anywhere]">
              {row.original.medicine_sku}
            </span>
          </div>
        ),
      },
      {
        accessorKey: 'quantity',
        header: 'Ordered',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="whitespace-nowrap tabular-nums">{units(row.original.quantity)}</span>
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
    ],
    [canOpenStock],
  )

  const backLink = (
    <Link
      to="/pharmacy/purchase-orders"
      className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1.5 transition-colors"
    >
      <ArrowLeft className="size-4" /> Back to purchase orders
    </Link>
  )

  if (isError) {
    const status = error instanceof ApiError ? error.status : undefined
    // An id that is not a UUID is a 422 on `path.order_id` (§9.8): to the user
    // that is an order that does not exist, not a form to correct.
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
                  ? 'Your account is not allowed to read purchase orders. Ask an administrator if you need access.'
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
            <PurchaseOrderStatusBadge status={order.status} />
          </div>
          <p className="font-body text-body-md text-on-surface-variant mt-1 [overflow-wrap:anywhere]">
            Purchase order to {order.vendor_name}
          </p>
        </div>
        <PurchaseOrderActions order={order} />
      </header>

      <StatusNote order={order} />

      <InfoCard title="Details">
        <Detail label="Vendor" value={order.vendor_name} />
        <Detail label="Created" value={formatDateTime(order.created_at)} />
        <Detail label="Marked as sent" value={order.ordered_at && formatDateTime(order.ordered_at)} />
        <Detail label="Received" value={order.received_at && formatDateTime(order.received_at)} />
        <Detail label="Total ordered" value={formatMoney(order.total_amount)} />
        <Detail label="Notes" value={order.notes} />
      </InfoCard>

      <section className="space-y-3" aria-label="Medicines ordered">
        <h2 className="font-display text-title-lg text-primary font-bold">Medicines ordered</h2>
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
