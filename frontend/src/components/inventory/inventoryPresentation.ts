import type {
  AdjustStockReason,
  InventoryLocationKind,
  InventoryOrderStatus,
  MovementReason,
} from '@/api/inventory'

type Variant = 'neutral' | 'primary' | 'accent' | 'success' | 'warning' | 'critical' | 'error'

/** The kinds of place that hold stock, in the order a picker lists them (docs/18-API_CONTRACTS.md §10.3). */
export const LOCATION_KINDS: InventoryLocationKind[] = ['store', 'ward', 'icu', 'ot']

export const LOCATION_KIND: Record<InventoryLocationKind, { label: string }> = {
  store: { label: 'Store' },
  ward: { label: 'Ward' },
  icu: { label: 'ICU' },
  ot: { label: 'Operating theatre' },
}

/** Every reason a ledger entry can carry, as the movements filter lists them (§10.5). */
export const MOVEMENT_REASONS: MovementReason[] = [
  'received',
  'consumed',
  'transferred_in',
  'transferred_out',
  'adjusted',
  'expired',
]

export const MOVEMENT_REASON: Record<MovementReason, { label: string; variant: Variant }> = {
  received: { label: 'Received', variant: 'success' },
  consumed: { label: 'Used', variant: 'neutral' },
  transferred_in: { label: 'Transferred in', variant: 'accent' },
  transferred_out: { label: 'Transferred out', variant: 'accent' },
  adjusted: { label: 'Adjusted', variant: 'warning' },
  expired: { label: 'Expired write-off', variant: 'critical' },
}

/** The two reasons the adjust endpoint accepts (`AdjustStockRequest.reason`); the rest are written by other routes. */
export const ADJUST_REASONS: AdjustStockReason[] = ['adjusted', 'expired']

export const ADJUST_REASON: Record<AdjustStockReason, { label: string }> = {
  adjusted: { label: 'Count correction' },
  expired: { label: 'Expired write-off' },
}

/** Purchase-order statuses in lifecycle order (§10.7). The same look as Pharmacy's orders (§9.8). */
export const INVENTORY_ORDER_STATUSES: InventoryOrderStatus[] = ['draft', 'sent', 'received', 'cancelled']

export const INVENTORY_ORDER_STATUS: Record<InventoryOrderStatus, { label: string; variant: Variant }> = {
  draft: { label: 'Draft', variant: 'neutral' },
  sent: { label: 'Sent', variant: 'accent' },
  received: { label: 'Received', variant: 'success' },
  cancelled: { label: 'Cancelled', variant: 'error' },
}

/** A decimal string as the API writes one: an optional sign, digits, an optional fraction. */
const DECIMAL = /^([+-]?)(\d+)(?:\.(\d+))?$/

/**
 * A quantity's digits with the padding the wire adds taken off: "300.00" is
 * "300", "2.50" is "2.5". Done on the string, so no precision is invented or
 * lost and no rounding happens; digits are not grouped, matching how the
 * server words its own messages. Anything that is not a decimal string comes
 * back unchanged.
 */
function plain(quantity: string): { negative: boolean; digits: string } | null {
  const match = DECIMAL.exec(quantity.trim())
  if (!match) return null
  const whole = match[2].replace(/^0+(?=\d)/, '')
  const fraction = (match[3] ?? '').replace(/0+$/, '')
  const digits = fraction ? `${whole}.${fraction}` : whole
  // "-0.00" is zero, not a removal.
  return { negative: match[1] === '-' && /[1-9]/.test(digits), digits }
}

function withUnit(figure: string, unit?: string): string {
  return unit ? `${figure} ${unit}` : figure
}

/**
 * A quantity for display: "300.00" → "300", "2.50" → "2.5", and with the
 * item's `unit_of_measure`, "300 piece". The unit is the item's own wording and
 * is never pluralised — "box of 100" has no plural to guess.
 */
export function quantityLabel(quantity: string, unit?: string): string {
  const parsed = plain(quantity)
  if (!parsed) return withUnit(quantity, unit)
  return withUnit(`${parsed.negative ? '−' : ''}${parsed.digits}`, unit)
}

/**
 * A ledger change for display, always signed: "40.00" → "+40", "-2.50" →
 * "−2.5" (a true minus sign, which a screen reader announces). Zero carries no
 * sign.
 */
export function signedQuantityLabel(quantityChange: string, unit?: string): string {
  const parsed = plain(quantityChange)
  if (!parsed) return withUnit(quantityChange, unit)
  const sign = parsed.negative ? '−' : /[1-9]/.test(parsed.digits) ? '+' : ''
  return withUnit(`${sign}${parsed.digits}`, unit)
}

/** Whether a ledger change removed stock — for wording, never for arithmetic. */
export function isRemoval(quantityChange: string): boolean {
  return plain(quantityChange)?.negative ?? false
}

/** Whether a quantity is zero ("0.00"), e.g. a suggested order for an item that is not low. */
export function isZeroQuantity(quantity: string): boolean {
  const parsed = plain(quantity)
  return parsed !== null && !/[1-9]/.test(parsed.digits)
}

const PLAIN_DATE = /^(\d{4})-(\d{2})-(\d{2})$/

/**
 * A `YYYY-MM-DD` date — a batch's expiry — as a medium date ("30 Oct 2026").
 * `new Date('2026-10-30')` is midnight UTC, which is the 29th anywhere west of
 * Greenwich; the parts are read as a local calendar date instead, so the day
 * shown is the day the server sent. Echoes the input if it is not such a date.
 */
export function plainDateLabel(date: string): string {
  const match = PLAIN_DATE.exec(date)
  if (!match) return date
  const local = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]))
  return Number.isNaN(local.getTime()) ? date : local.toLocaleDateString(undefined, { dateStyle: 'medium' })
}
