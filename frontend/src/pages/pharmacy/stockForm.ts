import { z } from 'zod'
import type { AdjustStockInput, ReceiveBatchInput } from '@/api/pharmacy'
import { formatDate } from '@/lib/format'
import { MONEY_PATTERN } from '@/lib/money'
import { PHARMACY_CODE, PHARMACY_CODE_MESSAGE } from './medicineForm'

/** The API's cap on one receipt and on one adjustment, in either direction. */
const MAX_UNITS = 1_000_000

/** A count of whole units as typed: 1 to 1,000,000, never zero. */
const wholeUnits = z
  .string()
  .trim()
  .min(1, 'Enter how many units')
  .refine((v) => /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= MAX_UNITS, 'Whole units, 1–1,000,000')

/**
 * Taking stock in directly (docs/18-API_CONTRACTS.md §9.3). The expiry date is
 * only checked for being a date: whether it has passed is the server's call,
 * made on the hospital's own date, which need not be this browser's.
 */
export const receiveSchema = z.object({
  batch_number: z
    .string()
    .trim()
    .min(1, 'Enter the batch number')
    .max(50, 'Keep the batch number to 50 characters')
    .regex(PHARMACY_CODE, PHARMACY_CODE_MESSAGE),
  expiry_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'Enter the expiry date'),
  quantity: wholeUnits,
  cost_per_unit: z
    .string()
    .trim()
    .min(1, 'Enter the cost per unit')
    .regex(MONEY_PATTERN, 'Enter an amount such as 1.20'),
})

export type ReceiveValues = z.infer<typeof receiveSchema>

export const EMPTY_RECEIPT: ReceiveValues = {
  batch_number: '',
  expiry_date: '',
  quantity: '',
  cost_per_unit: '',
}

export function toReceiveBody(values: ReceiveValues): ReceiveBatchInput {
  return {
    batch_number: values.batch_number.toUpperCase(),
    expiry_date: values.expiry_date,
    quantity: Number(values.quantity),
    cost_per_unit: values.cost_per_unit,
  }
}

export const ADJUST_DIRECTIONS = [
  { value: 'remove', label: 'Remove units' },
  { value: 'add', label: 'Add units' },
] as const

/** The two reasons the adjust endpoint accepts, as the ledger records them. */
export const ADJUST_REASONS = [
  { value: 'adjusted', label: 'Count correction' },
  { value: 'expired', label: 'Expired stock written off' },
] as const

/**
 * Correcting a batch's count (§9.3). The API takes one signed number; the form
 * asks for a direction and a count, so a minus sign is never typed or missed.
 * `quantity_change` keeps the API's name so a 422 on it lands under the count.
 *
 * Whether the batch holds enough to remove is not checked here: the count on
 * screen may be out of date, and the server answers with the real one.
 */
export const adjustSchema = z
  .object({
    direction: z.enum(['remove', 'add']),
    quantity_change: wholeUnits,
    reason: z.enum(['adjusted', 'expired']),
    note: z
      .string()
      .trim()
      .min(1, 'Say why the count is being corrected')
      .max(500, 'Keep the note to 500 characters'),
  })
  .superRefine((values, ctx) => {
    // The API's own rule: "Writing off expired stock must remove units."
    if (values.reason === 'expired' && values.direction === 'add') {
      ctx.addIssue({
        code: 'custom',
        path: ['reason'],
        message: 'Expired stock can only be removed — choose Count correction to add units',
      })
    }
  })

export type AdjustValues = z.infer<typeof adjustSchema>

export const EMPTY_ADJUSTMENT: AdjustValues = {
  direction: 'remove',
  quantity_change: '',
  reason: 'adjusted',
  note: '',
}

export function toAdjustBody(values: AdjustValues): AdjustStockInput {
  const count = Number(values.quantity_change)
  return {
    quantity_change: values.direction === 'remove' ? -count : count,
    reason: values.reason,
    note: values.note,
  }
}

/**
 * An expiry date for display. It is a calendar day with no time or zone
 * (`YYYY-MM-DD`). Read as local midnight it shows as that same day wherever
 * the viewer is; read bare it is taken as UTC and shows as the day before
 * anywhere west of Greenwich.
 */
export function expiryDateLabel(date: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(date) ? formatDate(`${date}T00:00:00`) : date
}

/**
 * The server's `days_to_expiry` in words. It is counted on the hospital's own
 * date (§9.3), so it is worded from the number as given — never recounted from
 * the expiry date here.
 */
export function expiryInWords(daysToExpiry: number): string {
  if (daysToExpiry === 0) return 'expires today'
  const days = Math.abs(daysToExpiry)
  const span = `${days.toLocaleString()} ${days === 1 ? 'day' : 'days'}`
  return daysToExpiry > 0 ? `expires in ${span}` : `expired ${span} ago`
}
