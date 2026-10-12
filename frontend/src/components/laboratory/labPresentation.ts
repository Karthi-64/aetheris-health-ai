import type { AppointmentStatus } from '@/api/appointments'
import type { LabOrderItem, LabOrderPriority, LabOrderStatus, LabResultFlag } from '@/api/lab'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

/** Order statuses in lifecycle order (docs/18-API_CONTRACTS.md §8.5). */
export const LAB_ORDER_STATUSES: LabOrderStatus[] = [
  'ordered',
  'collected',
  'in_progress',
  'results_entered',
  'released',
  'cancelled',
]

export const LAB_ORDER_STATUS: Record<LabOrderStatus, { label: string; variant: Variant }> = {
  ordered: { label: 'Ordered', variant: 'neutral' },
  collected: { label: 'Sample collected', variant: 'accent' },
  in_progress: { label: 'In progress', variant: 'warning' },
  results_entered: { label: 'Awaiting release', variant: 'primary' },
  released: { label: 'Released', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
}

export const LAB_PRIORITIES: LabOrderPriority[] = ['routine', 'urgent', 'stat']

export const LAB_PRIORITY: Record<LabOrderPriority, { label: string; variant: Variant }> = {
  routine: { label: 'Routine', variant: 'neutral' },
  urgent: { label: 'Urgent', variant: 'warning' },
  stat: { label: 'STAT', variant: 'critical' },
}

/** How the server's flag is worded. The flag is the server's; nothing is judged here. */
export const LAB_FLAG: Record<LabResultFlag, { label: string; variant: Variant }> = {
  normal: { label: 'Normal', variant: 'success' },
  low: { label: 'Low', variant: 'warning' },
  high: { label: 'High', variant: 'warning' },
  critical: { label: 'Critical', variant: 'critical' },
}

/** Statuses the cancel endpoint accepts: anything before release (§8.5). */
export const LAB_CANCELLABLE: ReadonlySet<LabOrderStatus> = new Set([
  'ordered',
  'collected',
  'in_progress',
  'results_entered',
])

/** Statuses in which results can be entered or re-entered (§8.5). */
export const LAB_RESULTS_ENTERABLE: ReadonlySet<LabOrderStatus> = new Set([
  'collected',
  'in_progress',
  'results_entered',
])

/**
 * The range a result was judged against, as the server stored it. Bounds
 * arrive as four-decimal strings ("12.0000"); trailing zeros are dropped for
 * reading and nothing else is done to them.
 */
export function referenceRangeLabel(item: Pick<LabOrderItem, 'reference_low' | 'reference_high'>): string | null {
  const trim = (v: string) => (v.includes('.') ? v.replace(/0+$/, '').replace(/\.$/, '') : v)
  const { reference_low: low, reference_high: high } = item
  if (low !== null && high !== null) return `${trim(low)} – ${trim(high)}`
  if (low !== null) return `≥ ${trim(low)}`
  if (high !== null) return `≤ ${trim(high)}`
  return null
}

/** "5.2 g/dL", or just the value when the test has no unit. */
export function resultLabel(value: string, unit: string | null): string {
  return unit ? `${value} ${unit}` : value
}

/** Visits the API refuses to take a lab order for (§8.4). */
const UNORDERABLE: ReadonlySet<AppointmentStatus> = new Set(['cancelled', 'no_show'])

/** Whether tests can be ordered for a visit in this status. */
export function canOrderLabTestsFor(status: AppointmentStatus): boolean {
  return !UNORDERABLE.has(status)
}

/** The visit an order is placed for. The patient and doctor are the visit's — the API takes neither. */
export interface OrderableVisit {
  id: string
  patient_name: string
  doctor_name: string
  scheduled_start: string
}
