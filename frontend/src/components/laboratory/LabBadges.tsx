import { TriangleAlert } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import type { LabOrder, LabOrderPriority, LabOrderStatus, LabResultFlag } from '@/api/lab'
import { LAB_FLAG, LAB_ORDER_STATUS, LAB_PRIORITY } from './labPresentation'

export function LabOrderStatusBadge({ status }: { status: LabOrderStatus }) {
  const s = LAB_ORDER_STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

export function LabPriorityBadge({ priority }: { priority: LabOrderPriority }) {
  const p = LAB_PRIORITY[priority]
  return <Badge variant={p.variant}>{p.label}</Badge>
}

/**
 * The server's verdict on a result. `null` means no reference range applied to
 * this patient (or the test is free text) — which is not the same as normal,
 * so it is said in words rather than left looking like a pass.
 */
export function LabResultFlagBadge({ flag }: { flag: LabResultFlag | null }) {
  if (flag === null) {
    return <span className="font-body text-outline text-xs">No reference range</span>
  }
  const f = LAB_FLAG[flag]
  return (
    <Badge variant={f.variant}>
      {flag === 'critical' && <TriangleAlert className="mr-1 inline size-3" aria-hidden />}
      {f.label}
    </Badge>
  )
}

/**
 * A worklist marker from the order's own `has_critical` / `has_abnormal`
 * (docs/18-API_CONTRACTS.md §8.5). Text as well as colour.
 */
export function LabAbnormalMarker({ order }: { order: Pick<LabOrder, 'has_abnormal' | 'has_critical'> }) {
  if (order.has_critical) {
    return (
      <Badge variant="critical">
        <TriangleAlert className="mr-1 inline size-3" aria-hidden />
        Critical result
      </Badge>
    )
  }
  if (order.has_abnormal) return <Badge variant="warning">Abnormal result</Badge>
  return null
}
