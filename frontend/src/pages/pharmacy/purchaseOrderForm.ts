import { z } from 'zod'
import type {
  CreatePurchaseOrderInput,
  PurchaseOrder,
  PurchaseOrderItem,
  ReceiptLineInput,
} from '@/api/pharmacy'
import { MONEY_PATTERN } from '@/lib/money'

/** An order takes 1–50 lines and a receipt 1–50 batches (`MAX_LINES`, docs/18-API_CONTRACTS.md §9.8). */
export const MAX_ORDER_LINES = 50
/**
 * The 50 is for the whole receipt, across every order line and every split —
 * and an order is received once, so there is no second receipt for the rest.
 */
export const MAX_RECEIPT_ROWS = 50

const MAX_QUANTITY = 1_000_000

/** Whole units, 1–1,000,000: what the API takes for an ordered or a received quantity. */
const isQuantity = (v: string) => /^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= MAX_QUANTITY

/** An amount in hundredths, exactly — no float. Only for text that matches `MONEY_PATTERN`. */
function hundredths(amount: string): bigint {
  const [whole, fraction = ''] = amount.split('.')
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
}

/** The most a `NUMERIC(15,2)` column holds, in hundredths. */
const MAX_HUNDREDTHS = 999_999_999_999_999n

/** An amount the API accepts: zero or more, two decimals at most, fifteen digits in all. */
const isMoney = (v: string) => MONEY_PATTERN.test(v) && hundredths(v) <= MAX_HUNDREDTHS

// ── Drafting an order ───────────────────────────────────────────────────────

const orderLineSchema = z.object({
  medicine_id: z.string().min(1, 'Choose a medicine'),
  quantity: z.string().trim().refine(isQuantity, 'Whole units, 1–1,000,000'),
  unit_price: z.string().trim().refine(isMoney, 'Enter an amount such as 1.20'),
})

/**
 * The new-order form (`POST /purchase-orders`, §9.8). Its fields carry the
 * request body's own names, so a 422 naming `vendor_id` or
 * `items.<i>.medicine_id` lands under that field.
 */
export const purchaseOrderSchema = z
  .object({
    vendor_id: z.string().min(1, 'Choose a vendor'),
    notes: z.string().trim().max(2000, 'Keep the notes to 2,000 characters or fewer'),
    items: z
      .array(orderLineSchema)
      .min(1, 'An order needs at least one line')
      .max(MAX_ORDER_LINES, `An order can hold at most ${MAX_ORDER_LINES} lines`),
  })
  .superRefine((values, ctx) => {
    const seen = new Set<string>()
    values.items.forEach((line, index) => {
      // The picker already refuses a second pick; this is the form's own check.
      if (line.medicine_id && seen.has(line.medicine_id)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'medicine_id'],
          message: 'This medicine is already on the order',
        })
      }
      seen.add(line.medicine_id)
      // The API does not check a line's total against the column that stores
      // it, so one that does not fit comes back as a 500 with no reason.
      if (
        isQuantity(line.quantity) &&
        isMoney(line.unit_price) &&
        hundredths(line.unit_price) * BigInt(line.quantity) > MAX_HUNDREDTHS
      ) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'quantity'],
          message: "This line's total is more than can be recorded",
        })
      }
    })
  })

export type PurchaseOrderValues = z.infer<typeof purchaseOrderSchema>
export type OrderLineValues = PurchaseOrderValues['items'][number]

/** The price starts empty: it is what the vendor charges, not the medicine's selling price. */
export const EMPTY_ORDER_LINE: OrderLineValues = { medicine_id: '', quantity: '', unit_price: '' }

export const EMPTY_ORDER: PurchaseOrderValues = {
  vendor_id: '',
  notes: '',
  items: [EMPTY_ORDER_LINE],
}

/**
 * What the lines typed so far add up to, for checking before the order is
 * drafted. A preview only: the order's real total is the server's. Lines that
 * are not complete yet are left out and counted in `incomplete`.
 */
export function previewOrderTotal(items: readonly OrderLineValues[]): {
  amount: string
  incomplete: number
} {
  let total = 0n
  let incomplete = 0
  for (const line of items) {
    const quantity = line.quantity.trim()
    const price = line.unit_price.trim()
    if (isQuantity(quantity) && isMoney(price)) total += hundredths(price) * BigInt(quantity)
    else incomplete += 1
  }
  return { amount: `${total / 100n}.${String(total % 100n).padStart(2, '0')}`, incomplete }
}

/**
 * The POST body, and nothing else: the request model forbids unknown keys, so
 * no status, number or total is sent. A blank note is left out.
 */
export function toCreateBody(values: PurchaseOrderValues): CreatePurchaseOrderInput {
  return {
    vendor_id: values.vendor_id,
    ...(values.notes ? { notes: values.notes } : {}),
    items: values.items.map((line) => ({
      medicine_id: line.medicine_id,
      quantity: Number(line.quantity),
      unit_price: line.unit_price,
    })),
  }
}

// ── Receiving an order ──────────────────────────────────────────────────────

/** A batch number as the API accepts it once uppercased: `^[A-Z0-9][A-Z0-9_./-]*$`. */
const BATCH_NUMBER = /^[A-Za-z0-9][A-Za-z0-9_./-]*$/

const receiptRowSchema = z.object({
  /** The ORDER LINE this batch was delivered against — not the medicine. */
  po_item_id: z.string(),
  batch_number: z
    .string()
    .trim()
    .min(1, 'Enter the batch number')
    .max(50, 'Keep the batch number to 50 characters or fewer')
    .regex(BATCH_NUMBER, 'Letters, digits and - _ . / only, starting with a letter or digit'),
  // Only that a date was given: whether it has passed is judged by the server,
  // on the hospital's calendar rather than this browser's.
  expiry_date: z
    .string()
    .min(1, 'Enter the expiry date')
    .regex(/^\d{4}-\d{2}-\d{2}$/, 'Enter a valid date'),
  quantity: z.string().trim().refine(isQuantity, 'Whole units, 1–1,000,000'),
  cost_per_unit: z
    .string()
    .trim()
    .refine((v) => v === '' || isMoney(v), 'Enter an amount such as 1.20, or leave it blank'),
})

const receiptBaseSchema = z.object({
  /**
   * One row per delivered batch, as ONE flat list in the order's own line
   * order — exactly the request's `items`. Because the form and the request
   * share both the name and the positions, a 422 naming `items.<i>.expiry_date`
   * lands on the row it is about with no translation.
   */
  items: z.array(receiptRowSchema),
  /**
   * Never sent. The {@link shortfallKey} the user ticked "cannot be received
   * later" for, or empty. Holding the shortfall itself rather than a yes/no
   * means a tick given for one shortfall does not cover a different one.
   */
  acknowledged: z.string(),
})

export type ReceiptValues = z.infer<typeof receiptBaseSchema>
export type ReceiptRowValues = ReceiptValues['items'][number]

type OrderedLine = Pick<PurchaseOrderItem, 'id' | 'quantity'>

/** How what is entered for one order line compares with what was ordered. */
export type LineReceipt =
  /** No batch rows: nothing arrived, and the line is left out of the receipt. */
  | { state: 'none' }
  /** A quantity is missing or not a number yet, so there is nothing to compare. */
  | { state: 'unknown' }
  | { state: 'short' | 'exact' | 'over'; entered: number }

/**
 * Compare a line's entered quantities with its order. The server makes no
 * such comparison — a short or an over receipt is accepted as it is — which is
 * why the form has to say it before the order is closed.
 */
export function lineReceipt(ordered: number, quantities: readonly string[]): LineReceipt {
  if (quantities.length === 0) return { state: 'none' }
  const trimmed = quantities.map((q) => q.trim())
  if (!trimmed.every(isQuantity)) return { state: 'unknown' }
  const entered = trimmed.reduce((sum, q) => sum + Number(q), 0)
  return { state: entered < ordered ? 'short' : entered > ordered ? 'over' : 'exact', entered }
}

/** The entered quantities of one order line's rows. */
export function quantitiesFor(lineId: string, rows: readonly ReceiptRowValues[]): string[] {
  return rows.filter((row) => row.po_item_id === lineId).map((row) => row.quantity)
}

/** Order lines the receipt leaves short or out altogether: what can never be received afterwards. */
export function shortLines<T extends OrderedLine>(lines: readonly T[], rows: readonly ReceiptRowValues[]): T[] {
  return lines.filter((line) => {
    const { state } = lineReceipt(line.quantity, quantitiesFor(line.id, rows))
    return state === 'none' || state === 'short'
  })
}

/**
 * Names the receipt's shortfall — which lines are short and what was entered
 * for each — or is empty when nothing is short. It is what the confirmation
 * tick is given for.
 */
export function shortfallKey(lines: readonly OrderedLine[], rows: readonly ReceiptRowValues[]): string {
  return shortLines(lines, rows)
    .map((line) => {
      const receipt = lineReceipt(line.quantity, quantitiesFor(line.id, rows))
      return `${line.id}=${receipt.state === 'short' ? receipt.entered : 0}`
    })
    .join(',')
}

/**
 * The receipt form for one order (`POST /purchase-orders/{id}/receive`, §9.8).
 * The rules that span rows are the API's: at least one batch, at most fifty
 * in the whole receipt, and a batch number once per order line.
 */
export function receiptSchema(lines: readonly OrderedLine[]) {
  return receiptBaseSchema.superRefine((values, ctx) => {
    if (values.items.length === 0) {
      ctx.addIssue({
        code: 'custom',
        path: ['items'],
        message: 'Enter at least one batch that arrived. An order nothing arrived for cannot be received.',
      })
    }
    if (values.items.length > MAX_RECEIPT_ROWS) {
      ctx.addIssue({
        code: 'custom',
        path: ['items'],
        message: `A receipt can hold at most ${MAX_RECEIPT_ROWS} batches`,
      })
    }
    // The server uppercases a batch number before comparing, so "b1" and "B1"
    // on one line are the same batch. The same number on two lines is fine:
    // those are two medicines.
    const seen = new Set<string>()
    values.items.forEach((row, index) => {
      if (!row.batch_number) return
      const key = `${row.po_item_id} ${row.batch_number.toUpperCase()}`
      if (seen.has(key)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'batch_number'],
          message: 'This batch is already listed for this medicine — enter it once, with its whole quantity',
        })
      }
      seen.add(key)
    })
    // One receipt closes the order, so a shortfall has to be confirmed as it
    // stands now. A complete or an over receipt needs no tick.
    const shortfall = shortfallKey(lines, values.items)
    if (shortfall && values.acknowledged !== shortfall) {
      ctx.addIssue({
        code: 'custom',
        path: ['acknowledged'],
        message: 'Tick the box to confirm before the order is closed',
      })
    }
  })
}

/** A fresh batch row for an order line. */
export function receiptRow(lineId: string, quantity = ''): ReceiptRowValues {
  return { po_item_id: lineId, batch_number: '', expiry_date: '', quantity, cost_per_unit: '' }
}

/** One row per order line, its quantity starting at what was ordered. */
export function receiptDefaults(order: Pick<PurchaseOrder, 'items'>): ReceiptValues {
  return {
    items: order.items.map((line) => receiptRow(line.id, String(line.quantity))),
    acknowledged: '',
  }
}

/**
 * Where a new row for `lineId` goes in the flat list: after the last row of
 * that line, or of any line ordered before it. That keeps each line's rows
 * together and in the order's own line order, which the positions sent to the
 * API depend on.
 */
export function insertionIndex(
  rowLineIds: readonly string[],
  lineIds: readonly string[],
  lineId: string,
): number {
  const upToLine = new Set(lineIds.slice(0, lineIds.indexOf(lineId) + 1))
  let at = 0
  rowLineIds.forEach((id, index) => {
    if (upToLine.has(id)) at = index + 1
  })
  return at
}

/**
 * The receipt's `items`, row for row as the form holds them. The batch number
 * goes in capitals, as the server stores it. A blank cost is left out — not
 * sent empty — and the server then uses the order line's price.
 */
export function toReceiptBody(values: ReceiptValues): ReceiptLineInput[] {
  return values.items.map((row) => ({
    po_item_id: row.po_item_id,
    batch_number: row.batch_number.toUpperCase(),
    expiry_date: row.expiry_date,
    quantity: Number(row.quantity),
    ...(row.cost_per_unit ? { cost_per_unit: row.cost_per_unit } : {}),
  }))
}
