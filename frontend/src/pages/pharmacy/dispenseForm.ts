import { z } from 'zod'
import type { DispenseInput, Prescription, PrescriptionItem } from '@/api/pharmacy'
import type { FieldError } from '@/lib/apiErrors'
import { PRESCRIPTION_DISPENSABLE } from '@/components/pharmacy/pharmacyPresentation'

/**
 * The lines the pharmacy can still dispense: catalog lines with units
 * outstanding (docs/18-API_CONTRACTS.md §9.5). A free-text line is never one —
 * it is not stocked, and its `quantity_remaining` stays at what was prescribed
 * for ever, even on a prescription that is fully dispensed.
 */
export function outstandingLines(prescription: Pick<Prescription, 'items'>): PrescriptionItem[] {
  return prescription.items.filter((i) => i.medicine_id !== null && i.quantity_remaining > 0)
}

/**
 * Whether the dispense endpoint has anything to do for this prescription. A
 * prescription of free-text lines only stays `active` and is refused with
 * "Nothing on this prescription is left to dispense." — so it is not offered.
 */
export function canBeDispensed(prescription: Pick<Prescription, 'status' | 'items'>): boolean {
  return PRESCRIPTION_DISPENSABLE.has(prescription.status) && outstandingLines(prescription).length > 0
}

/** Whether the server reports enough dispensable units today to finish this line. */
export function isCovered(line: PrescriptionItem): boolean {
  return line.available_quantity !== null && line.available_quantity >= line.quantity_remaining
}

export type Availability =
  | { state: 'in_stock' }
  | { state: 'short'; medicines: string[] }
  | { state: 'nothing' }

/**
 * A waiting prescription's availability, read off the two numbers the API
 * returns on each outstanding line — `available_quantity` against
 * `quantity_remaining`. No stock is worked out here. `null` for a prescription
 * that is no longer waiting.
 */
export function availabilityOf(prescription: Pick<Prescription, 'status' | 'items'>): Availability | null {
  if (!PRESCRIPTION_DISPENSABLE.has(prescription.status)) return null
  const lines = outstandingLines(prescription)
  if (lines.length === 0) return { state: 'nothing' }
  const short = lines.filter((line) => !isCovered(line))
  return short.length === 0
    ? { state: 'in_stock' }
    : { state: 'short', medicines: short.map((line) => line.medicine_name) }
}

const WHOLE = /^\d+$/

/** One row per outstanding line, in the prescription's order. Quantities stay text until sent. */
const valuesSchema = z.object({
  lines: z.array(z.object({ prescription_item_id: z.string(), quantity: z.string().trim() })),
  notes: z.string().trim().max(2000, 'Keep the reason under 2,000 characters'),
})

export type DispenseValues = z.infer<typeof valuesSchema>

/**
 * What the form opens with: each line at what is left of it, or at what the
 * server says is available today when that is less. A line with nothing
 * available opens at 0, which leaves it out.
 */
export function dispenseDefaults(lines: PrescriptionItem[]): DispenseValues {
  return {
    lines: lines.map((line) => ({
      prescription_item_id: line.id,
      quantity: String(
        line.available_quantity === null
          ? line.quantity_remaining
          : Math.min(line.quantity_remaining, line.available_quantity),
      ),
    })),
    notes: '',
  }
}

const typedFor = (values: DispenseValues, line: PrescriptionItem) =>
  values.lines.find((row) => row.prescription_item_id === line.id)?.quantity.trim() ?? ''

/**
 * Whether what is entered would leave any line with units outstanding — the
 * case in which the API requires `notes` (§9.5).
 */
export function isPartial(lines: PrescriptionItem[], values: DispenseValues): boolean {
  return lines.some((line) => {
    const typed = typedFor(values, line)
    return !WHOLE.test(typed) || Number(typed) < line.quantity_remaining
  })
}

/** Whether no line has a quantity above zero, so there is nothing to send. */
export function asksForNothing(lines: PrescriptionItem[], values: DispenseValues): boolean {
  return lines.every((line) => !(Number(typedFor(values, line)) > 0))
}

/**
 * The form's rules, built for the lines as they stand now: a whole number from
 * 0 to what is left of each line, at least one line above 0, and a reason
 * whenever anything is left outstanding.
 */
export function dispenseSchema(lines: PrescriptionItem[]) {
  return valuesSchema.superRefine((values, ctx) => {
    let valid = true
    values.lines.forEach((row, index) => {
      const line = lines.find((l) => l.id === row.prescription_item_id)
      if (!line) return
      const message = !WHOLE.test(row.quantity)
        ? 'Enter a whole number — 0 leaves this medicine out'
        : Number(row.quantity) > line.quantity_remaining
          ? `Only ${line.quantity_remaining} left to dispense`
          : null
      if (message) {
        valid = false
        ctx.addIssue({ code: 'custom', path: ['lines', index, 'quantity'], message })
      }
    })
    if (!valid) return
    if (asksForNothing(lines, values)) {
      ctx.addIssue({ code: 'custom', path: ['lines'], message: 'Enter a quantity for at least one medicine' })
    } else if (isPartial(lines, values) && !values.notes) {
      ctx.addIssue({
        code: 'custom',
        path: ['notes'],
        message: 'Say why the rest is not being dispensed now',
      })
    }
  })
}

/**
 * The body for `POST /prescriptions/{id}/dispense`.
 *
 * With every line at all that is left of it, no `items` are sent and the
 * server dispenses everything outstanding. Otherwise the lines above zero are
 * named — a quantity of 0 is a 422, so those are left out.
 */
export function toDispenseBody(lines: PrescriptionItem[], values: DispenseValues): DispenseInput {
  const notes = values.notes ? { notes: values.notes } : {}
  if (!isPartial(lines, values)) return notes
  return {
    items: lines
      .map((line) => ({ prescription_item_id: line.id, quantity: Number(typedFor(values, line)) }))
      .filter((item) => item.quantity > 0),
    ...notes,
  }
}

export interface PlacedErrors {
  /** Errors that belong under a row's quantity, by the row's index in the form. */
  rows: { index: number; message: string }[]
  notes: string | null
  /** Errors that name nothing on the form. */
  other: string[]
}

/**
 * Where a refused dispense's field errors belong on the form. The API names
 * fields in the body that was sent — `items.<i>` counts the items sent, which
 * leaves out the rows at 0 — so each is traced back to its row through the
 * `prescription_item_id` that was sent at that position.
 */
export function placeDispenseErrors(
  errors: FieldError[],
  sent: DispenseInput,
  values: DispenseValues,
): PlacedErrors {
  const placed: PlacedErrors = { rows: [], notes: null, other: [] }
  for (const fe of errors) {
    if (fe.field === 'notes') {
      placed.notes = fe.message
      continue
    }
    const [, at] = /^items\.(\d+)(?:\.|$)/.exec(fe.field) ?? []
    const itemId = at === undefined ? undefined : sent.items?.[Number(at)]?.prescription_item_id
    const index = itemId === undefined ? -1 : values.lines.findIndex((row) => row.prescription_item_id === itemId)
    if (index >= 0) placed.rows.push({ index, message: fe.message })
    else placed.other.push(fe.message)
  }
  return placed
}
