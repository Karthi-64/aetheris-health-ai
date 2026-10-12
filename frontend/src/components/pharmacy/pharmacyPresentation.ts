import type { AppointmentStatus } from '@/api/appointments'
import type {
  Medicine,
  MedicineBatch,
  Prescription,
  PrescriptionStatus,
  PurchaseOrderStatus,
} from '@/api/pharmacy'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

/** Prescription statuses in lifecycle order (docs/18-API_CONTRACTS.md §9.4). */
export const PRESCRIPTION_STATUSES: PrescriptionStatus[] = [
  'active',
  'partially_dispensed',
  'dispensed',
  'cancelled',
]

export const PRESCRIPTION_STATUS: Record<PrescriptionStatus, { label: string; variant: Variant }> = {
  active: { label: 'To dispense', variant: 'accent' },
  partially_dispensed: { label: 'Partly dispensed', variant: 'warning' },
  dispensed: { label: 'Dispensed', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
}

/** Statuses the dispense endpoint accepts (`DISPENSABLE_STATUSES` in the pharmacy models). */
export const PRESCRIPTION_DISPENSABLE: ReadonlySet<PrescriptionStatus> = new Set([
  'active',
  'partially_dispensed',
])

/**
 * Whether the cancel endpoint will accept this prescription: only while
 * nothing has been dispensed from it (§9.4).
 */
export function canCancelPrescription(prescription: Pick<Prescription, 'status' | 'items'>): boolean {
  return prescription.status === 'active' && prescription.items.every((i) => i.quantity_dispensed === 0)
}

/** Purchase-order statuses in lifecycle order (§9.8). */
export const PURCHASE_ORDER_STATUSES: PurchaseOrderStatus[] = ['draft', 'sent', 'received', 'cancelled']

export const PURCHASE_ORDER_STATUS: Record<PurchaseOrderStatus, { label: string; variant: Variant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  sent: { label: 'Sent', variant: 'accent' },
  received: { label: 'Received', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
}

/** Visits the API refuses to take a prescription for (§9.4). */
const UNPRESCRIBABLE: ReadonlySet<AppointmentStatus> = new Set(['cancelled', 'no_show'])

/** Whether a prescription can be written for a visit in this status. */
export function canPrescribeFor(status: AppointmentStatus): boolean {
  return !UNPRESCRIBABLE.has(status)
}

/** The visit a prescription is written in. The patient and doctor are the visit's — the API takes neither. */
export interface PrescribableVisit {
  id: string
  patient_name: string
  doctor_name: string
  scheduled_start: string
}

/** "500 mg · tablet", or null when the catalog holds neither. */
export function medicineDetail(medicine: Pick<Medicine, 'strength' | 'form'>): string | null {
  const parts = [medicine.strength, medicine.form].filter((v): v is string => !!v)
  return parts.length > 0 ? parts.join(' · ') : null
}

export type BatchState = 'recalled' | 'expired' | 'empty' | 'expiring' | 'ok'

/**
 * A batch's state for display, read from the server's own flags — `is_expired`,
 * `expires_soon` and `is_dispensable` are worked out on the hospital's date,
 * so no date is compared here.
 */
export function batchState(batch: MedicineBatch): BatchState {
  if (batch.is_recalled) return 'recalled'
  if (batch.is_expired) return 'expired'
  if (batch.quantity_on_hand === 0) return 'empty'
  if (batch.expires_soon) return 'expiring'
  return 'ok'
}

export const BATCH_STATE: Record<BatchState, { label: string; variant: Variant }> = {
  recalled: { label: 'Recalled', variant: 'error' },
  expired: { label: 'Expired', variant: 'critical' },
  empty: { label: 'Empty', variant: 'neutral' },
  expiring: { label: 'Expires soon', variant: 'warning' },
  ok: { label: 'Dispensable', variant: 'success' },
}

/** "12 units" / "1 unit". */
export function units(n: number): string {
  return `${n.toLocaleString()} ${n === 1 ? 'unit' : 'units'}`
}
