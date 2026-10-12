import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight } from 'lucide-react'
import { AwaitingApprovalBadge, InvoiceStatusBadge } from '@/components/billing/InvoiceStatusBadge'
import type { InvoiceItem, InvoiceSummary, Payment, Refund } from '@/api/billing'
import { formatDate, formatMoney, formatTime } from '@/lib/format'
import { PAYMENT_METHOD_LABEL } from './billing'

function Money({ value, currency, strong }: { value: string; currency: string; strong?: boolean }) {
  return (
    <span className={strong ? 'font-semibold tabular-nums' : 'tabular-nums'}>
      {formatMoney(value, currency)}
    </span>
  )
}

const dateTime = (iso: string) => `${formatDate(iso)}, ${formatTime(iso)}`

/** The invoice list. Every amount is the server's figure, rendered as sent. */
export const invoiceColumns: ColumnDef<InvoiceSummary>[] = [
  {
    accessorKey: 'invoice_number',
    header: 'Invoice',
    enableSorting: false,
    cell: ({ row }) =>
      row.original.invoice_number ? (
        <span className="font-mono text-primary">{row.original.invoice_number}</span>
      ) : (
        <span className="text-outline">Not yet issued</span>
      ),
  },
  {
    accessorKey: 'patient_name',
    header: 'Patient',
    enableSorting: false,
    cell: ({ row }) => <span className="font-semibold">{row.original.patient_name}</span>,
  },
  {
    id: 'date',
    header: 'Date',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="tabular-nums">
        {formatDate(row.original.issued_at ?? row.original.created_at)}
      </span>
    ),
  },
  {
    accessorKey: 'status',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="flex flex-wrap items-center gap-1.5">
        <InvoiceStatusBadge status={row.original.status} />
        {row.original.discount_pending_approval && <AwaitingApprovalBadge />}
      </span>
    ),
  },
  {
    accessorKey: 'total',
    header: 'Total',
    enableSorting: false,
    cell: ({ row }) => <Money value={row.original.total} currency={row.original.currency} />,
  },
  {
    accessorKey: 'amount_paid',
    header: 'Paid',
    enableSorting: false,
    cell: ({ row }) => <Money value={row.original.amount_paid} currency={row.original.currency} />,
  },
  {
    accessorKey: 'balance_due',
    header: 'Outstanding',
    enableSorting: false,
    cell: ({ row }) => (
      <Money value={row.original.balance_due} currency={row.original.currency} strong />
    ),
  },
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    cell: ({ row }) => (
      <Link
        to={`/billing/${row.original.id}`}
        aria-label={`View invoice for ${row.original.patient_name}${
          row.original.invoice_number ? ` (${row.original.invoice_number})` : ''
        }`}
        className="text-outline hover:text-secondary inline-flex items-center gap-1 transition-colors"
      >
        View <ChevronRight className="size-4" />
      </Link>
    ),
  },
]

export function invoiceItemColumns(currency: string): ColumnDef<InvoiceItem>[] {
  return [
    { accessorKey: 'description', header: 'Item', enableSorting: false },
    {
      accessorKey: 'quantity',
      header: 'Qty',
      enableSorting: false,
      cell: ({ row }) => <span className="tabular-nums">{row.original.quantity}</span>,
    },
    {
      accessorKey: 'unit_price',
      header: 'Unit price',
      enableSorting: false,
      cell: ({ row }) => <Money value={row.original.unit_price} currency={currency} />,
    },
    {
      accessorKey: 'tax_rate',
      header: 'Tax',
      enableSorting: false,
      cell: ({ row }) => <span className="tabular-nums">{row.original.tax_rate}%</span>,
    },
    {
      accessorKey: 'line_total',
      header: 'Amount',
      enableSorting: false,
      cell: ({ row }) => <Money value={row.original.line_total} currency={currency} strong />,
    },
  ]
}

export function paymentColumns(currency: string): ColumnDef<Payment>[] {
  return [
    {
      accessorKey: 'received_at',
      header: 'Received',
      enableSorting: false,
      cell: ({ row }) => <span className="tabular-nums">{dateTime(row.original.received_at)}</span>,
    },
    {
      accessorKey: 'method',
      header: 'Method',
      enableSorting: false,
      cell: ({ row }) => PAYMENT_METHOD_LABEL[row.original.method],
    },
    {
      accessorKey: 'reference',
      header: 'Reference',
      enableSorting: false,
      cell: ({ row }) => row.original.reference ?? <span className="text-outline-variant">—</span>,
    },
    {
      accessorKey: 'amount',
      header: 'Amount',
      enableSorting: false,
      cell: ({ row }) => <Money value={row.original.amount} currency={currency} strong />,
    },
  ]
}

export function refundColumns(currency: string): ColumnDef<Refund>[] {
  return [
    {
      accessorKey: 'refunded_at',
      header: 'Refunded',
      enableSorting: false,
      cell: ({ row }) => <span className="tabular-nums">{dateTime(row.original.refunded_at)}</span>,
    },
    {
      accessorKey: 'method',
      header: 'Method',
      enableSorting: false,
      cell: ({ row }) => PAYMENT_METHOD_LABEL[row.original.method],
    },
    { accessorKey: 'reason', header: 'Reason', enableSorting: false },
    {
      accessorKey: 'amount',
      header: 'Amount',
      enableSorting: false,
      cell: ({ row }) => <Money value={row.original.amount} currency={currency} strong />,
    },
  ]
}
