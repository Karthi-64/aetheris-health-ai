import { z } from 'zod'
import type { CreateInventoryItemInput, InventoryItem, UpdateInventoryItemInput } from '@/api/inventory'

/**
 * What the API takes as a SKU: letters, digits and `- _ . /`, starting with a
 * letter or digit (`_CODE_PATTERN` in `backend/app/schemas/inventory.py`). The
 * server trims and uppercases the value before checking it, so lower case is
 * accepted here — but ASCII only. It measures the 50-character limit before
 * uppercasing, and a letter that grows when uppercased ("ß" becomes "SS") can
 * pass that check and then overflow the column.
 */
const ITEM_CODE = /^[A-Za-z0-9][A-Za-z0-9_./-]*$/

/** The API's ceiling on a reorder point and a target (`le=10_000_000`). */
const MAX_LEVEL = 10_000_000

/**
 * A reorder point or a target as typed: blank for "not set", or a whole number
 * from 0 to ten million. The API takes an integer — "2.5" is a 422, never
 * rounded — so nothing but digits is accepted and nothing is rounded here.
 */
const level = (what: string) =>
  z
    .string()
    .trim()
    .refine((v) => v === '' || /^\d+$/.test(v), `Enter the ${what} as a whole number, or leave it blank`)
    .refine(
      (v) => !/^\d+$/.test(v) || (v.length <= 8 && Number(v) <= MAX_LEVEL),
      `Keep the ${what} to 10000000 or less`,
    )

/**
 * Every field but the SKU (docs/18-API_CONTRACTS.md §10.3). Limits mirror the
 * API's: name 1–200, category ≤ 100, unit of measure 1–30. Text is trimmed
 * before its length is checked: the API measures the raw text, so what is sent
 * has to be the trimmed value.
 */
const fields = {
  name: z.string().trim().min(1, 'Enter a name').max(200, 'Keep the name to 200 characters'),
  category: z.string().trim().max(100, 'Keep the category to 100 characters'),
  unit_of_measure: z
    .string()
    .trim()
    .min(1, 'Enter what one unit is, e.g. piece or box of 100')
    .max(30, 'Keep the unit to 30 characters'),
  is_batch_tracked: z.boolean(),
  reorder_point: level('reorder point'),
  target_stock: level('target stock'),
  is_active: z.boolean(),
}

/** The message the API itself gives for the rule below, in plainer words. */
const TARGET_BELOW_REORDER = 'The target stock must not be less than the reorder point'

/**
 * A reorder should aim above the level that triggers it: the API refuses a
 * target below the reorder point (`_check_levels` in
 * `backend/app/schemas/inventory.py`). A new item is judged on the two figures
 * in the request, which are the two in the form, so the same comparison is made
 * here and reported under the target.
 */
function checkLevels(
  values: { reorder_point: string; target_stock: string },
  ctx: z.RefinementCtx,
) {
  const reorder = toLevel(values.reorder_point)
  const target = toLevel(values.target_stock)
  if (reorder !== null && target !== null && target < reorder) {
    ctx.addIssue({ code: 'custom', path: ['target_stock'], message: TARGET_BELOW_REORDER })
  }
}

/** The form for a new item. */
export const createItemSchema = z
  .object({
    sku: z
      .string()
      .trim()
      .min(1, 'Enter a SKU')
      .max(50, 'Keep the SKU to 50 characters')
      .regex(ITEM_CODE, 'Letters, digits and - _ . / only, starting with a letter or digit'),
    ...fields,
  })
  .superRefine(checkLevels)

/**
 * The form for an existing item. Its SKU is shown but can never be sent
 * (§10.3), so it is not checked: a field the user cannot change must not block
 * saving the ones they can. The levels are not compared here either — see
 * `levelsRefusal`.
 */
export const editItemSchema = z.object({ sku: z.string(), ...fields })

export type ItemValues = z.infer<typeof createItemSchema>

/**
 * A new item is counted in "unit"s and is not batch-tracked, the API's own
 * defaults, and has no reorder point — so it never alerts until one is set.
 */
export const EMPTY_ITEM: ItemValues = {
  sku: '',
  name: '',
  category: '',
  unit_of_measure: 'unit',
  is_batch_tracked: false,
  reorder_point: '',
  target_stock: '',
  is_active: true,
}

export function toItemValues(item: InventoryItem): ItemValues {
  return {
    sku: item.sku,
    name: item.name,
    category: item.category ?? '',
    unit_of_measure: item.unit_of_measure,
    is_batch_tracked: item.is_batch_tracked,
    reorder_point: item.reorder_point === null ? '' : String(item.reorder_point),
    target_stock: item.target_stock === null ? '' : String(item.target_stock),
    is_active: item.is_active,
  }
}

/** A validated level as the API takes it: an integer, or null for "not set". */
function toLevel(value: string): number | null {
  const digits = value.trim()
  return /^\d+$/.test(digits) ? Number(digits) : null
}

/**
 * The POST body. `is_active` is never sent: the request model forbids unknown
 * keys, and a new item is active. An optional field left blank goes as null,
 * which the API accepts for all three (`CreateInventoryItemRequest`).
 */
export function toCreateBody(values: ItemValues): CreateInventoryItemInput {
  return {
    sku: values.sku.toUpperCase(),
    name: values.name,
    category: values.category || null,
    unit_of_measure: values.unit_of_measure,
    is_batch_tracked: values.is_batch_tracked,
    reorder_point: toLevel(values.reorder_point),
    target_stock: toLevel(values.target_stock),
  }
}

/**
 * The PATCH body: only what changed. `sku` and `is_batch_tracked` are never
 * sent — the API answers 422 to either (§10.3). A cleared category, reorder
 * point or target goes as null, which clears it; `name`, `unit_of_measure` and
 * `is_active` cannot be null and are never sent as one. Levels are compared as
 * numbers, so retyping 20 as "020" is not a change.
 */
export function toUpdateBody(opened: ItemValues, values: ItemValues): UpdateInventoryItemInput {
  const body: UpdateInventoryItemInput = {}
  if (values.name !== opened.name) body.name = values.name
  if (values.category !== opened.category) body.category = values.category || null
  if (values.unit_of_measure !== opened.unit_of_measure) body.unit_of_measure = values.unit_of_measure
  if (toLevel(values.reorder_point) !== toLevel(opened.reorder_point)) {
    body.reorder_point = toLevel(values.reorder_point)
  }
  if (toLevel(values.target_stock) !== toLevel(opened.target_stock)) {
    body.target_stock = toLevel(values.target_stock)
  }
  if (values.is_active !== opened.is_active) body.is_active = values.is_active
  return body
}

/**
 * Why an edit must not be sent, or null when it may be.
 *
 * On an edit the API compares the levels in the request with the item as it is
 * saved now for whichever one the request leaves out (`update_item` in
 * `backend/app/services/inventory_service.py`), and that may no longer be what
 * the form was opened with. So the form is only sure of the answer when it
 * sends both: then the two figures judged are the two typed. With one, the
 * figure on screen for the other may be stale — refusing on it would block a
 * save the server accepts — so the server decides, and its 422 lands under the
 * target.
 */
export function levelsRefusal(body: UpdateInventoryItemInput): string | null {
  const { reorder_point: reorder, target_stock: target } = body
  return typeof reorder === 'number' && typeof target === 'number' && target < reorder ? TARGET_BELOW_REORDER : null
}
