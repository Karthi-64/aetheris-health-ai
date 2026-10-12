import { z } from 'zod'
import type { CreateMedicineInput, Medicine, UpdateMedicineInput } from '@/api/pharmacy'
import { MONEY_PATTERN } from '@/lib/money'

/**
 * What the API takes as a SKU or a batch number: letters, digits and `- _ . /`,
 * starting with a letter or digit (`backend/app/schemas/pharmacy.py`). The
 * server uppercases the value before checking it, so lower case is accepted
 * here — but ASCII only. It measures the 50-character limit before
 * uppercasing, and a letter that grows when uppercased ("ß" becomes "SS") can
 * pass that check and then overflow the column, which answers 500.
 */
export const PHARMACY_CODE = /^[A-Za-z0-9][A-Za-z0-9_./-]*$/

export const PHARMACY_CODE_MESSAGE = 'Letters, digits and - _ . / only, starting with a letter or digit'

const optionalText = (max: number, what: string) =>
  z.string().trim().max(max, `Keep the ${what} to ${max} characters`)

/**
 * Every field but the SKU (docs/18-API_CONTRACTS.md §9.3). Limits mirror the
 * API's: name ≤ 200; generic name ≤ 200; strength ≤ 50; form ≤ 50; ATC code
 * ≤ 20. The price is a decimal string with at most two decimals — a third is a
 * 422, never rounded — and zero is allowed.
 */
const fields = {
  name: z.string().trim().min(1, 'Enter a name').max(200, 'Keep the name to 200 characters'),
  generic_name: optionalText(200, 'generic name'),
  strength: optionalText(50, 'strength'),
  form: optionalText(50, 'form'),
  atc_code: optionalText(20, 'ATC code'),
  unit_price: z
    .string()
    .trim()
    .min(1, 'Enter the selling price per unit')
    .regex(MONEY_PATTERN, 'Enter an amount such as 2.50'),
  requires_prescription: z.boolean(),
  is_active: z.boolean(),
}

/** The form for a new medicine. */
export const createMedicineSchema = z.object({
  sku: z
    .string()
    .trim()
    .min(1, 'Enter a SKU')
    .max(50, 'Keep the SKU to 50 characters')
    .regex(PHARMACY_CODE, PHARMACY_CODE_MESSAGE),
  ...fields,
})

/**
 * The form for an existing medicine. Its SKU is shown but can never be sent
 * (§9.3), so it is not checked: a field the user cannot change must not block
 * saving the ones they can.
 */
export const editMedicineSchema = z.object({ sku: z.string(), ...fields })

export type MedicineValues = z.infer<typeof createMedicineSchema>

/**
 * A new medicine starts with no price — one typed is better than a default
 * saved by accident — and marked as needing a prescription, the API's default.
 */
export const EMPTY_MEDICINE: MedicineValues = {
  sku: '',
  name: '',
  generic_name: '',
  strength: '',
  form: '',
  atc_code: '',
  unit_price: '',
  requires_prescription: true,
  is_active: true,
}

export function toMedicineValues(medicine: Medicine): MedicineValues {
  return {
    sku: medicine.sku,
    name: medicine.name,
    generic_name: medicine.generic_name ?? '',
    strength: medicine.strength ?? '',
    form: medicine.form ?? '',
    atc_code: medicine.atc_code ?? '',
    unit_price: medicine.unit_price,
    requires_prescription: medicine.requires_prescription,
    is_active: medicine.is_active,
  }
}

/**
 * The POST body. `is_active` is never sent: the API rejects it on create
 * (a new medicine is active). An optional field left blank goes as null.
 */
export function toCreateBody(values: MedicineValues): CreateMedicineInput {
  return {
    sku: values.sku.toUpperCase(),
    name: values.name,
    generic_name: values.generic_name || null,
    strength: values.strength || null,
    form: values.form || null,
    atc_code: values.atc_code || null,
    unit_price: values.unit_price,
    requires_prescription: values.requires_prescription,
  }
}

const OPTIONAL_TEXT = ['generic_name', 'strength', 'form', 'atc_code'] as const

/**
 * The PATCH body: only what changed. `sku` is never sent — the API answers 422
 * to it (§9.3). A cleared optional field goes as null, which clears it; the
 * other fields cannot be null and are never sent as one.
 */
export function toUpdateBody(opened: MedicineValues, values: MedicineValues): UpdateMedicineInput {
  const body: UpdateMedicineInput = {}
  if (values.name !== opened.name) body.name = values.name
  for (const key of OPTIONAL_TEXT) {
    if (values[key] !== opened[key]) body[key] = values[key] || null
  }
  if (values.unit_price !== opened.unit_price) body.unit_price = values.unit_price
  if (values.requires_prescription !== opened.requires_prescription) {
    body.requires_prescription = values.requires_prescription
  }
  if (values.is_active !== opened.is_active) body.is_active = values.is_active
  return body
}
