import { z } from 'zod'
import type {
  CreateInventoryOrderInput,
  InventoryOrder,
  InventoryOrderItem,
  ReceiveInventoryOrderInput,
} from '@/api/inventory'
import { MONEY_PATTERN } from '@/lib/money'

/** An order takes 1–50 lines and a receipt 1–50 rows (`MAX_LINES`, docs/18-API_CONTRACTS.md §10.7). */
export const MAX_ORDER_LINES = 50
/**
 * The 50 is for the whole receipt, across every order line and every split —
 * and an order is received once, so there is no second receipt for the rest.
 */
export const MAX_RECEIPT_ROWS = 50

/** A decimal as the API takes one for a quantity: digits, at most two decimals. */
const QUANTITY_PATTERN = /^\d+(\.\d{1,2})?$/

/** A decimal in hundredths, exactly — no float. Only for text that matches one of the patterns above. */
function hundredths(amount: string): bigint {
  const [whole, fraction = ''] = amount.split('.')
  return BigInt(whole) * 100n + BigInt(fraction.padEnd(2, '0'))
}

/** The most a `NUMERIC(12,2)` quantity holds, in hundredths. */
const MAX_QUANTITY_HUNDREDTHS = 999_999_999_999n
/** The most a `NUMERIC(15,2)` amount holds, in hundredths. */
const MAX_MONEY_HUNDREDTHS = 999_999_999_999_999n

/**
 * A quantity the API accepts (`Quantity` in `backend/app/schemas/inventory.py`):
 * above zero, two decimals at most, twelve digits in all. Unlike a medicine, a
 * consumable can be ordered in a fraction of its unit.
 */
function isQuantity(v: string): boolean {
  if (!QUANTITY_PATTERN.test(v)) return false
  const value = hundredths(v)
  return value > 0n && value <= MAX_QUANTITY_HUNDREDTHS
}

/** An amount the API accepts (`Money`): zero or more, two decimals at most, fifteen digits in all. */
const isMoney = (v: string) => MONEY_PATTERN.test(v) && hundredths(v) <= MAX_MONEY_HUNDREDTHS

const QUANTITY_MESSAGE = 'More than 0, with at most two decimal places'

// ── Drafting an order ───────────────────────────────────────────────────────

const orderLineSchema = z.object({
  item_id: z.string().min(1, 'Choose an item'),
  quantity: z.string().trim().refine(isQuantity, QUANTITY_MESSAGE),
  unit_price: z.string().trim().refine(isMoney, 'Enter an amount such as 1.20'),
})

/**
 * The new-order form (`POST /inventory/purchase-orders`, §10.7). Its fields
 * carry the request body's own names, so a 422 naming `vendor_id` or
 * `items.<i>.item_id` lands under that field.
 */
export const inventoryOrderSchema = z
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
      // The request model refuses an item listed twice (a 422 on `items`). The
      // picker already refuses a second pick; this is the form's own check.
      if (line.item_id && seen.has(line.item_id)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'item_id'],
          message: 'This item is already on the order',
        })
      }
      seen.add(line.item_id)
      // The API does not check a line's total against the column that stores
      // it, so one that does not fit comes back as a 500 with no reason.
      if (
        isQuantity(line.quantity) &&
        isMoney(line.unit_price) &&
        hundredths(line.unit_price) * hundredths(line.quantity) > MAX_MONEY_HUNDREDTHS * 100n
      ) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'quantity'],
          message: "This line's total is more than can be recorded",
        })
      }
    })
  })

export type InventoryOrderValues = z.infer<typeof inventoryOrderSchema>
export type InventoryOrderLineValues = InventoryOrderValues['items'][number]

/** The price starts empty: an item carries no price of its own, so it is whatever the vendor quoted. */
export const EMPTY_ORDER_LINE: InventoryOrderLineValues = { item_id: '', quantity: '', unit_price: '' }

export const EMPTY_ORDER: InventoryOrderValues = {
  vendor_id: '',
  notes: '',
  items: [EMPTY_ORDER_LINE],
}

/**
 * The POST body, and nothing else: the request model forbids unknown keys, so
 * no status, number or total is sent. Quantity and price go as the decimal
 * text that was typed, never through a float. A blank note is left out.
 */
export function toCreateBody(values: InventoryOrderValues): CreateInventoryOrderInput {
  return {
    vendor_id: values.vendor_id,
    ...(values.notes ? { notes: values.notes } : {}),
    items: values.items.map((line) => ({
      item_id: line.item_id,
      quantity: line.quantity,
      unit_price: line.unit_price,
    })),
  }
}

// ── Receiving an order ──────────────────────────────────────────────────────

/** A batch number as the API accepts it once uppercased: `^[A-Z0-9][A-Z0-9_./-]*$`. */
const BATCH_NUMBER = /^[A-Za-z0-9][A-Za-z0-9_./-]*$/
const PLAIN_DATE = /^\d{4}-\d{2}-\d{2}$/

const receiptRowSchema = z.object({
  /** The ORDER LINE these units were delivered against — not the item. */
  po_item_id: z.string(),
  quantity: z.string().trim().refine(isQuantity, QUANTITY_MESSAGE),
  /** Blank for an item that is not batch-tracked. Whether one is needed is judged per line, below. */
  batch_number: z.string().trim(),
  /** Blank when the batch has no expiry: the API does not require one, even for a tracked item. */
  expiry_date: z.string(),
})

const receiptBaseSchema = z.object({
  /** Where everything on the receipt goes: one location for the whole order. */
  location_id: z.string().min(1, 'Choose where the goods are received into'),
  /**
   * One row per delivered batch (or per untracked line), as ONE flat list in
   * the order's own line order — exactly the request's `items`. Because the
   * form and the request share both the name and the positions, a 422 naming
   * `items.<i>.batch_number` lands on the row it is about with no translation.
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

type OrderedLine = Pick<InventoryOrderItem, 'id' | 'item_id' | 'quantity'>

/**
 * Whether an item is batch-tracked, or `undefined` when that is not known —
 * the order line does not say (`InventoryPurchaseOrderItemResponse` has no
 * such field), so it comes from the item's own record, which may not have
 * loaded or may not be readable by this user.
 */
export type TrackingLookup = (itemId: string) => boolean | undefined

/** How what is entered for one order line compares with what was ordered. */
export type LineReceipt =
  /** No rows: nothing arrived, and the line is left out of the receipt. */
  | { state: 'none' }
  /** A quantity is missing or not a number yet, so there is nothing to compare. */
  | { state: 'unknown' }
  /** `entered` and `difference` are decimal strings with two places, like the API's. */
  | { state: 'short' | 'exact' | 'over'; entered: string; difference: string }

const fromHundredths = (value: bigint) => `${value / 100n}.${String(value % 100n).padStart(2, '0')}`

/**
 * Compare a line's entered quantities with its order. The server makes no
 * such comparison — a short or an over receipt is accepted as it is
 * (`inventory_po_service.py`, `receive_purchase_order`) — which is why the
 * form has to say it before the order is closed. A comparison only: what is
 * sent is each row's quantity as typed.
 */
export function lineReceipt(ordered: string, quantities: readonly string[]): LineReceipt {
  if (quantities.length === 0) return { state: 'none' }
  const trimmed = quantities.map((q) => q.trim())
  if (!trimmed.every(isQuantity) || !QUANTITY_PATTERN.test(ordered)) return { state: 'unknown' }
  const entered = trimmed.reduce((sum, q) => sum + hundredths(q), 0n)
  const wanted = hundredths(ordered)
  const gap = entered < wanted ? wanted - entered : entered - wanted
  return {
    state: entered < wanted ? 'short' : entered > wanted ? 'over' : 'exact',
    entered: fromHundredths(entered),
    difference: fromHundredths(gap),
  }
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
 * The receipt form for one order (`POST /inventory/purchase-orders/{id}/receive`,
 * §10.7). The rules are the API's: a location, at least one row, at most fifty
 * in the whole receipt, a batch number on every row of a batch-tracked item
 * and on none of an untracked one, and a batch once per order line.
 *
 * `isTracked` is asked when the form is submitted, not when this is built, so
 * it sees item records that loaded after the dialog opened. Where it does not
 * know, the batch number is left to the server, whose 422 names the row.
 */
export function receiptSchema(lines: readonly OrderedLine[], isTracked: TrackingLookup) {
  const itemOf = new Map(lines.map((line) => [line.id, line.item_id]))
  return receiptBaseSchema.superRefine((values, ctx) => {
    if (values.items.length === 0) {
      ctx.addIssue({
        code: 'custom',
        path: ['items'],
        message: 'Enter at least one line that arrived. An order nothing arrived for cannot be received.',
      })
    }
    if (values.items.length > MAX_RECEIPT_ROWS) {
      ctx.addIssue({
        code: 'custom',
        path: ['items'],
        message: `A receipt can hold at most ${MAX_RECEIPT_ROWS} rows`,
      })
    }
    const seen = new Set<string>()
    values.items.forEach((row, index) => {
      const tracked = isTracked(itemOf.get(row.po_item_id) ?? '')
      // An untracked item's batch and expiry inputs are not shown and are not
      // sent, so whatever they hold must not hold the receipt up.
      if (tracked === false) {
        // Its rows can still be two: a second one may have been added while
        // the item could not be checked. Sent as they are, both would be the
        // same (line, no batch) and the request model refuses the whole list
        // without naming the line. The quantity is the one input such a row
        // shows, so the reason goes under it.
        const key = `${row.po_item_id} `
        if (seen.has(key)) {
          ctx.addIssue({
            code: 'custom',
            path: ['items', index, 'quantity'],
            message: 'This item is not batch-tracked: enter the whole quantity in one row, and remove this one',
          })
        }
        seen.add(key)
        return
      }
      const batch = row.batch_number
      if (tracked === true && !batch) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'batch_number'],
          message: 'Enter the batch number: this item is batch-tracked',
        })
      }
      if (batch.length > 50) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'batch_number'],
          message: 'Keep the batch number to 50 characters or fewer',
        })
      } else if (batch && !BATCH_NUMBER.test(batch)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'batch_number'],
          message: 'Letters, digits and - _ . / only, starting with a letter or digit',
        })
      }
      // Only that it is a date: whether it has passed is judged by the server,
      // on the hospital's calendar rather than this browser's.
      if (row.expiry_date && !PLAIN_DATE.test(row.expiry_date)) {
        ctx.addIssue({ code: 'custom', path: ['items', index, 'expiry_date'], message: 'Enter a valid date' })
      } else if (row.expiry_date && !batch && tracked === undefined) {
        // Tracking is not known, and an expiry is only ever right with a
        // batch: a tracked item is refused for the missing batch number, and
        // an untracked one would have the date stamped on its single,
        // batch-less stock row (`receive_purchase_order` passes it on whatever
        // the tracking), which stops being usable the day it passes. So the
        // date is not sent alone — and not dropped silently either.
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'expiry_date'],
          message: 'An expiry date is recorded only with a batch number: enter the batch number, or clear the date',
        })
      }
      // The server uppercases a batch number before comparing, so "b1" and
      // "B1" on one line are the same batch — and two rows with no batch are
      // the same too. The same number on two lines is fine: two items.
      const key = `${row.po_item_id} ${batch.toUpperCase()}`
      if (seen.has(key)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'batch_number'],
          message: batch
            ? 'This batch is already listed for this item — enter it once, with its whole quantity'
            : 'Give each row of this item its own batch number, or enter the whole quantity in one row',
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

/** A fresh row for an order line. */
export function receiptRow(lineId: string, quantity = ''): ReceiptRowValues {
  return { po_item_id: lineId, quantity, batch_number: '', expiry_date: '' }
}

/**
 * One row per order line, its quantity starting at what was ordered — the
 * order keeps no "outstanding" figure, because it is received only once. The
 * location starts unchosen: nothing says where a delivery belongs.
 */
export function receiptDefaults(order: Pick<InventoryOrder, 'items'>): ReceiptValues {
  return {
    location_id: '',
    items: order.items.map((line) => receiptRow(line.id, plainQuantity(line.quantity))),
    acknowledged: '',
  }
}

/** "300.00" as "300", "2.50" as "2.5": the wire's padding taken off for an input, digit for digit. */
function plainQuantity(quantity: string): string {
  return quantity.includes('.') ? quantity.replace(/0+$/, '').replace(/\.$/, '') : quantity
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
 * The receive body, row for row as the form holds them. The quantity goes as
 * the decimal text that was typed. A batch number goes in capitals, as the
 * server stores it; a blank batch number or expiry is left out, not sent empty.
 * Neither is sent for an item known not to be batch-tracked: a batch number
 * there is a 422, and its row has no input for either. An expiry never goes
 * without a batch number: the server would accept it for an untracked item and
 * record it on that item's one batch-less stock row at the location.
 */
export function toReceiptBody(
  values: ReceiptValues,
  lines: readonly OrderedLine[],
  isTracked: TrackingLookup,
): ReceiveInventoryOrderInput {
  const itemOf = new Map(lines.map((line) => [line.id, line.item_id]))
  return {
    location_id: values.location_id,
    items: values.items.map((row) => {
      const batched = isTracked(itemOf.get(row.po_item_id) ?? '') !== false
      return {
        po_item_id: row.po_item_id,
        quantity: row.quantity,
        ...(batched && row.batch_number ? { batch_number: row.batch_number.toUpperCase() } : {}),
        ...(batched && row.batch_number && row.expiry_date ? { expiry_date: row.expiry_date } : {}),
      }
    }),
  }
}
