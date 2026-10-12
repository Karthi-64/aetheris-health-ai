import { Badge } from '@/components/ui/badge'
import type { InvoiceStatus } from '@/api/billing'
import { INVOICE_STATUS } from './invoiceStatus'

export function InvoiceStatusBadge({ status }: { status: InvoiceStatus }) {
  const s = INVOICE_STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

/** Shown beside the status while a draft's discount is waiting for an admin. */
export function AwaitingApprovalBadge() {
  return <Badge variant="warning">Awaiting approval</Badge>
}
