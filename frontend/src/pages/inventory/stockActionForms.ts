import { z } from 'zod'
import { ApiError } from '@/api/types'
import type {
  AdjustStockInput,
  ConsumeStockInput,
  InventoryItemRef,
  StockChange,
  StockMovement,
  StockRow,
  TransferStockInput,
} from '@/api/inventory'
import {
  ADJUST_REASON,
  quantityLabel,
  signedQuantityLabel,
} from '@/components/inventory/inventoryPresentation'

/*
 * The forms behind the three writes on stock (docs/18-API_CONTRACTS.md §10.5;
 * `ConsumeRequest`, `TransferRequest` and `AdjustStockRequest` in
 * `backend/app/schemas/inventory.py`). Each form names its fields as the API's
 * body does, so a 422 lands under the field it is about; the few fields the
 * API does not have (`item_name`, `batch_tracked`, …) are what the form needs
 * to word itself and are never sent.
 *
 * A quantity stays the string that was typed, from the input to the wire: it
 * is a decimal, and nothing here turns it into a number.
 */

// ── Shared rules ────────────────────────────────────────────────────────────

/** The answers the API writes for a write it refused: in each, nothing was changed. */
const REFUSALS = [400, 403, 404, 409, 422]

/**
 * True when a failed write may nonetheless have been applied: a 5xx, a timeout
 * or a dropped connection says nothing about whether the server committed
 * before the answer was lost. None of the three writes takes an
 * Idempotency-Key (docs/18-API_CONTRACTS.md §10.5), so sending it again would
 * move the stock a second time — the user is told to look before retrying.
 */
export function outcomeUnknown(err: unknown): boolean {
  return !(err instanceof ApiError && REFUSALS.includes(err.status ?? 0))
}

/**
 * What each dialog says when {@link outcomeUnknown}. Never "try again": the
 * ledger is where a write that did go through shows, so it is checked first.
 */
export const UNCONFIRMED = {
  consume:
    "Couldn't confirm that the use was recorded. Check Movements before trying again, so it isn't recorded twice. The confirmation has been cleared.",
  transfer:
    "Couldn't confirm that the stock was transferred. Check Movements before trying again, so it isn't moved twice.",
  adjust:
    "Couldn't confirm that the adjustment was saved. Check Movements before trying again, so it isn't applied twice.",
} as const

/** A positive decimal as typed: digits, then at most two decimal places. */
const QUANTITY = /^\d+(\.\d{1,2})?$/
/** `NUMERIC(12, 2)`: ten digits before the point (`Quantity`, schemas/inventory.py). */
const MAX_WHOLE_DIGITS = 10
/** Batch numbers once uppercased, as the API restricts them (`_CODE_PATTERN`). */
const BATCH = /^[A-Z0-9][A-Z0-9_./-]*$/
const MAX_BATCH = 50
const MAX_NOTE = 500

/**
 * How many units. The API takes any quantity above zero with up to two decimal
 * places, for every item — a fraction of a unit is allowed whether or not the
 * item is batch-tracked — so that is all that is checked. Whether the shelf
 * holds that many is the server's answer, not this form's.
 */
const quantity = z
  .string()
  .trim()
  .superRefine((value, ctx) => {
    if (!value) {
      ctx.addIssue({ code: 'custom', message: 'Enter how many units' })
    } else if (!QUANTITY.test(value)) {
      ctx.addIssue({ code: 'custom', message: 'A number with at most two decimal places, such as 2 or 2.5' })
    } else if (!/[1-9]/.test(value)) {
      ctx.addIssue({ code: 'custom', message: 'Must be more than zero' })
    } else if (value.split('.')[0].replace(/^0+(?=\d)/, '').length > MAX_WHOLE_DIGITS) {
      ctx.addIssue({ code: 'custom', message: 'That is more than the system can record' })
    }
  })

const optionalNote = z.string().trim().max(MAX_NOTE, 'Keep the note to 500 characters')

/** What a form holds about the chosen item. Only `item_id` is sent. */
const itemFields = {
  item_id: z.string().min(1, 'Choose the item'),
  item_name: z.string(),
  unit_of_measure: z.string(),
  /** Whether the item keeps stock per batch — decides if a batch can be named at all. */
  batch_tracked: z.boolean(),
}

/** The batch number as it is sent: trimmed and in capitals, as the server stores it. */
const batchOf = (value: string) => value.trim().toUpperCase()

/** Why a typed batch number cannot be one, or null when it can (or is blank). */
function batchProblem(value: string): string | null {
  const batch = batchOf(value)
  if (!batch) return null
  if (batch.length > MAX_BATCH) return 'Keep the batch number to 50 characters'
  return BATCH.test(batch) ? null : 'Letters, digits and - _ . / only, starting with a letter or a digit'
}

/** A quantity as the wire carries it: what was typed, without the spaces around it. */
const typedQuantity = (value: string) => value.trim()

/** The item a form starts on when it is opened from a stock row. */
export function itemRefOf(row: StockRow): InventoryItemRef {
  return { id: row.item_id, sku: row.item_sku, name: row.item_name, unit_of_measure: row.unit_of_measure }
}

/**
 * A stock row of a batch-tracked item always carries a batch number and one of
 * an untracked item never does: adjust and receive, the only routes that
 * create a row, refuse anything else (`_check_batch`, inventory_service.py).
 */
const itemDefaults = (row?: StockRow) => ({
  item_id: row?.item_id ?? '',
  item_name: row?.item_name ?? '',
  unit_of_measure: row?.unit_of_measure ?? '',
  batch_tracked: !!row && row.batch_number !== null,
})

// ── Consume ─────────────────────────────────────────────────────────────────

/**
 * Recording stock used (`POST /inventory/consume`). `batch_number` is optional
 * even for a batch-tracked item: left out, the server takes the earliest
 * expiry first; given, it takes from that batch only. For an untracked item a
 * batch number matches nothing and the answer is a 409, so it is not offered
 * and never sent.
 *
 * The write cannot be taken back and is not idempotent, so `confirmed` must
 * hold the exact sentence the user ticked — see {@link consumeStatement}.
 */
export const consumeSchema = z
  .object({
    ...itemFields,
    location_id: z.string().min(1, 'Choose where it was taken from'),
    location_name: z.string(),
    quantity,
    batch_number: z.string(),
    /** Empty for none. Offered only to a user who may list departments. */
    department_id: z.string(),
    note: optionalNote,
    confirmed: z.string(),
  })
  .superRefine((values, ctx) => {
    const problem = values.batch_tracked ? batchProblem(values.batch_number) : null
    if (problem) ctx.addIssue({ code: 'custom', path: ['batch_number'], message: problem })
    // Asked for only once there is something to confirm: until then the fields
    // above carry the errors, and a tick box that cannot be ticked blocks nothing.
    const statement = problem ? null : consumeStatement(values)
    if (statement && values.confirmed !== statement) {
      ctx.addIssue({ code: 'custom', path: ['confirmed'], message: 'Tick the box to confirm what will be removed' })
    }
  })

export type ConsumeValues = z.infer<typeof consumeSchema>

export function consumeDefaults(row?: StockRow): ConsumeValues {
  return {
    ...itemDefaults(row),
    location_id: row?.location_id ?? '',
    location_name: row?.location_name ?? '',
    quantity: '',
    batch_number: row?.batch_number ?? '',
    department_id: '',
    note: '',
    confirmed: '',
  }
}

/**
 * What a consume will remove, in one sentence — "Remove 2 box of IV cannula 20G
 * from General store" — or null while the item, the location or a usable
 * quantity is missing. It is built from the request about to be sent, so a
 * tick given for one sentence does not carry over to another: change the
 * quantity and the box has to be ticked again.
 */
export function consumeStatement(
  values: Pick<
    ConsumeValues,
    'item_id' | 'item_name' | 'unit_of_measure' | 'batch_tracked' | 'location_id' | 'location_name' | 'quantity' | 'batch_number'
  >,
): string | null {
  const amount = typedQuantity(values.quantity)
  if (!values.item_id || !values.location_id || !QUANTITY.test(amount) || !/[1-9]/.test(amount)) return null
  const batch = values.batch_tracked ? batchOf(values.batch_number) : ''
  return (
    `Remove ${quantityLabel(amount, values.unit_of_measure || undefined)} of ${values.item_name} ` +
    `from ${values.location_name}${batch ? `, from batch ${batch} only` : ''}`
  )
}

export function toConsumeBody(values: ConsumeValues): ConsumeStockInput {
  const batch = values.batch_tracked ? batchOf(values.batch_number) : ''
  const note = values.note.trim()
  return {
    item_id: values.item_id,
    location_id: values.location_id,
    quantity: typedQuantity(values.quantity),
    // An optional field left blank is left out: the API has no use for ''.
    ...(values.department_id ? { department_id: values.department_id } : {}),
    ...(batch ? { batch_number: batch } : {}),
    ...(note ? { note } : {}),
  }
}

// ── Transfer ────────────────────────────────────────────────────────────────

/**
 * Moving stock between two locations (`POST /inventory/transfer`). The batch
 * rule is consume's: optional, earliest expiry first without it. The API
 * refuses a transfer to the place it starts from (422, "A transfer needs two
 * different locations."), so that is caught here, under the destination.
 */
export const transferSchema = z
  .object({
    ...itemFields,
    from_location_id: z.string().min(1, 'Choose where it is taken from'),
    from_location_name: z.string(),
    to_location_id: z.string().min(1, 'Choose where it is sent'),
    to_location_name: z.string(),
    quantity,
    batch_number: z.string(),
    note: optionalNote,
  })
  .superRefine((values, ctx) => {
    if (values.to_location_id && values.to_location_id === values.from_location_id) {
      ctx.addIssue({
        code: 'custom',
        path: ['to_location_id'],
        message: 'Choose a different location — stock cannot be moved to where it already is',
      })
    }
    const problem = values.batch_tracked ? batchProblem(values.batch_number) : null
    if (problem) ctx.addIssue({ code: 'custom', path: ['batch_number'], message: problem })
  })

export type TransferValues = z.infer<typeof transferSchema>

export function transferDefaults(row?: StockRow): TransferValues {
  return {
    ...itemDefaults(row),
    from_location_id: row?.location_id ?? '',
    from_location_name: row?.location_name ?? '',
    to_location_id: '',
    to_location_name: '',
    quantity: '',
    batch_number: row?.batch_number ?? '',
    note: '',
  }
}

export function toTransferBody(values: TransferValues): TransferStockInput {
  const batch = values.batch_tracked ? batchOf(values.batch_number) : ''
  const note = values.note.trim()
  return {
    item_id: values.item_id,
    from_location_id: values.from_location_id,
    to_location_id: values.to_location_id,
    quantity: typedQuantity(values.quantity),
    ...(batch ? { batch_number: batch } : {}),
    ...(note ? { note } : {}),
  }
}

// ── Adjust ──────────────────────────────────────────────────────────────────

export const ADJUST_DIRECTIONS = [
  { value: 'remove', label: 'Remove units' },
  { value: 'add', label: 'Add units' },
] as const

/**
 * Correcting a count or writing off expired stock (`POST /inventory/adjust`).
 * The API takes one signed `quantity_change`; the form asks for a direction
 * and an amount, so a minus sign is never typed or missed. The field keeps the
 * API's name so a 422 on it lands under the amount.
 *
 * The API's rules, kept here: the note is required; a batch-tracked item needs
 * a batch number and an untracked one must not have one; "expired" only ever
 * removes. `expiry_date` is recorded only when the adjustment creates the
 * batch, which only an addition does, so it is asked for only then — and
 * whether a date has passed is the server's call, made on the hospital's date.
 *
 * Whether the row holds enough to remove is not checked: the count on screen
 * may be out of date, and the server answers with the real one (400).
 */
export const adjustSchema = z
  .object({
    ...itemFields,
    location_id: z.string().min(1, 'Choose the location'),
    location_name: z.string(),
    batch_number: z.string(),
    expiry_date: z.string(),
    direction: z.enum(['remove', 'add']),
    quantity_change: quantity,
    reason: z.enum(['adjusted', 'expired']),
    note: z.string().trim().min(1, 'Say why the count is changing').max(MAX_NOTE, 'Keep the note to 500 characters'),
  })
  .superRefine((values, ctx) => {
    if (values.batch_tracked) {
      const problem = batchOf(values.batch_number)
        ? batchProblem(values.batch_number)
        : 'This item is batch-tracked — enter the batch number'
      if (problem) ctx.addIssue({ code: 'custom', path: ['batch_number'], message: problem })
    }
    if (asksForExpiry(values) && values.expiry_date && !/^\d{4}-\d{2}-\d{2}$/.test(values.expiry_date)) {
      ctx.addIssue({ code: 'custom', path: ['expiry_date'], message: 'Enter a date, or leave it empty' })
    }
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

/** Whether an expiry date means anything for this adjustment: one that can create a batch. */
export function asksForExpiry(values: Pick<AdjustValues, 'batch_tracked' | 'direction'>): boolean {
  return values.batch_tracked && values.direction === 'add'
}

export function adjustDefaults(row?: StockRow): AdjustValues {
  return {
    ...itemDefaults(row),
    location_id: row?.location_id ?? '',
    location_name: row?.location_name ?? '',
    batch_number: row?.batch_number ?? '',
    expiry_date: '',
    direction: 'remove',
    quantity_change: '',
    // Opened on a batch the server reports as expired, the likely job is the write-off.
    reason: row?.is_expired ? 'expired' : 'adjusted',
    note: '',
  }
}

export function toAdjustBody(values: AdjustValues): AdjustStockInput {
  const amount = typedQuantity(values.quantity_change)
  const batch = values.batch_tracked ? batchOf(values.batch_number) : ''
  return {
    item_id: values.item_id,
    location_id: values.location_id,
    quantity_change: values.direction === 'remove' ? `-${amount}` : amount,
    reason: values.reason,
    note: values.note.trim(),
    ...(batch ? { batch_number: batch } : {}),
    ...(asksForExpiry(values) && values.expiry_date ? { expiry_date: values.expiry_date } : {}),
  }
}

/**
 * Whether a form opened from a stock row is still about that row — the same
 * item, place and batch — and so whether the quantity read with the row says
 * anything about what is being changed.
 */
export function stillOnRow(
  row: StockRow,
  values: Pick<AdjustValues, 'item_id' | 'location_id' | 'batch_number'>,
): boolean {
  return (
    values.item_id === row.item_id &&
    values.location_id === row.location_id &&
    batchOf(values.batch_number) === (row.batch_number ?? '')
  )
}

// ── What the server answered ────────────────────────────────────────────────

/** Location names by id, for wording a ledger entry: an entry names its place by id only. */
export type Places = Record<string, string | undefined>

/** A ledger change without its sign: the sign is in the wording around it. No arithmetic. */
const amountOf = (movement: StockMovement, unit: string) =>
  quantityLabel(movement.quantity_change.trim().replace(/^[+-]/, ''), unit)

/** The item's stock across the hospital after a write, as the server reports it. */
function hospitalNow(change: StockChange): string {
  const { summary } = change
  const unit = summary.item.unit_of_measure
  return (
    `Across the hospital: ${quantityLabel(summary.quantity_on_hand, unit)} on hand, ` +
    `${quantityLabel(summary.usable_quantity, unit)} usable` +
    (summary.is_low ? ' — low stock' : '')
  )
}

/**
 * The outcome of a consume, from its `StockChangeResponse`: each ledger entry
 * the server wrote — one per batch it drew on — and the item's stock
 * afterwards. Nothing is added up here; the entries are listed as they came.
 */
export function consumeResult(change: StockChange, places: Places): string {
  const { item } = change.summary
  const place = places[change.movements[0]?.location_id ?? ''] ?? 'the location'
  const taken = change.movements
    .map((m) => `${amountOf(m, item.unit_of_measure)}${m.batch_number ? ` from batch ${m.batch_number}` : ''}`)
    .join(', ')
  return `Recorded ${item.name} used at ${place}: ${taken}. ${hospitalNow(change)}`
}

/**
 * The outcome of a transfer. The response carries no balance for either
 * location — only the ledger entries, one out and one in per batch moved, and
 * the hospital-wide totals, which a transfer does not change. So the entries
 * themselves are shown, signed, against the place each belongs to; the
 * refreshed stock list shows what each place now holds.
 */
export function transferResult(change: StockChange, places: Places): string {
  const { item } = change.summary
  const byBatch = new Map<string, string[]>()
  for (const m of change.movements) {
    const entry = `${places[m.location_id] ?? 'Another location'} ${signedQuantityLabel(m.quantity_change, item.unit_of_measure)}`
    byBatch.set(m.batch_number ?? '', [...(byBatch.get(m.batch_number ?? '') ?? []), entry])
  }
  const moved = [...byBatch.entries()]
    .map(([batch, entries]) => `${batch ? `batch ${batch}: ` : ''}${entries.join(', ')}`)
    .join('; ')
  return `Moved ${item.name} — ${moved}`
}

/**
 * The outcome of an adjustment. The response does not say what the corrected
 * row now holds — only the change it recorded and the item's totals across
 * the hospital — so those are what is reported; the row's new quantity comes
 * with the refreshed list, never from arithmetic here.
 */
export function adjustResult(change: StockChange, places: Places): string {
  const { item } = change.summary
  const [entry] = change.movements
  if (!entry) return `Adjusted ${item.name}. ${hospitalNow(change)}`
  const reason = entry.reason === 'adjusted' || entry.reason === 'expired' ? ADJUST_REASON[entry.reason].label : 'Adjustment'
  return (
    `${reason} recorded for ${item.name} at ${places[entry.location_id] ?? 'the location'}` +
    `${entry.batch_number ? `, batch ${entry.batch_number}` : ''}: ` +
    `${signedQuantityLabel(entry.quantity_change, item.unit_of_measure)}. ${hospitalNow(change)}`
  )
}

// ── The ledger and the stock list ───────────────────────────────────────────

/**
 * What a ledger entry's `reference_type` means, for the four values the
 * services write (inventory_service.py, inventory_po_service.py). Any other
 * value is shown as it came.
 */
const REFERENCE_LABEL: Record<string, string> = {
  consumption: 'Use recorded',
  transfer: 'Transfer',
  adjustment: 'Adjustment',
  purchase_order: 'Purchase order',
}

export function referenceLabel(referenceType: string | null): string | null {
  if (!referenceType) return null
  return REFERENCE_LABEL[referenceType] ?? referenceType
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/**
 * An id read from the address bar, or undefined if it cannot be one. The API
 * answers 422 to a filter that is not a UUID, which would show a mistyped link
 * as a list that failed to load; such a value is treated as no filter.
 */
export function idParam(value: string | null): string | undefined {
  return value && UUID.test(value) ? value : undefined
}
