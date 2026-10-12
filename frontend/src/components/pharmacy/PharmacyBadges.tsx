import { Badge } from '@/components/ui/badge'
import type { MedicineBatch, PrescriptionStatus, PurchaseOrderStatus } from '@/api/pharmacy'
import {
  BATCH_STATE,
  PRESCRIPTION_STATUS,
  PURCHASE_ORDER_STATUS,
  batchState,
} from './pharmacyPresentation'

export function PrescriptionStatusBadge({ status }: { status: PrescriptionStatus }) {
  const s = PRESCRIPTION_STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

export function PurchaseOrderStatusBadge({ status }: { status: PurchaseOrderStatus }) {
  const s = PURCHASE_ORDER_STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

/** A batch's state, taken from the server's flags (see {@link batchState}). */
export function BatchStateBadge({ batch }: { batch: MedicineBatch }) {
  const s = BATCH_STATE[batchState(batch)]
  return <Badge variant={s.variant}>{s.label}</Badge>
}
