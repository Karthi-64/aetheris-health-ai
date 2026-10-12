import type { InvoiceStatus } from '@/api/billing'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

/** Label and badge colour for each invoice status (docs/18-API_CONTRACTS.md §6.4). */
export const INVOICE_STATUS: Record<InvoiceStatus, { label: string; variant: Variant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  issued: { label: 'Issued', variant: 'accent' },
  partially_paid: { label: 'Partially paid', variant: 'warning' },
  paid: { label: 'Paid', variant: 'success' },
  void: { label: 'Void', variant: 'error' },
  refunded: { label: 'Refunded', variant: 'critical' },
}

export const INVOICE_STATUSES = Object.keys(INVOICE_STATUS) as InvoiceStatus[]
