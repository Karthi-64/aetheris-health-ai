import { z } from 'zod'
import type { CreateVendorInput, UpdateVendorInput, Vendor } from '@/api/pharmacy'

/**
 * The vendor form (docs/18-API_CONTRACTS.md §9.7). Limits mirror the API's:
 * name 1–200, contact ≤ 200, address ≤ 1000, tax id ≤ 50.
 *
 * Every text field is trimmed here, before the length is checked. The API
 * measures the raw text — 200 characters followed by a space is refused — so
 * what is sent has to be the trimmed value.
 */
export const vendorSchema = z.object({
  name: z
    .string()
    .trim()
    .min(1, "Enter the vendor's name")
    .max(200, 'Keep the name to 200 characters or fewer'),
  contact: z.string().trim().max(200, 'Keep the contact to 200 characters or fewer'),
  address: z.string().trim().max(1000, 'Keep the address to 1,000 characters or fewer'),
  tax_id: z.string().trim().max(50, 'Keep the tax ID to 50 characters or fewer'),
  /** Edit only: `POST /vendors` rejects the key, so a new vendor never sends it. */
  is_active: z.boolean(),
})

export type VendorValues = z.infer<typeof vendorSchema>

export const EMPTY_VENDOR: VendorValues = {
  name: '',
  contact: '',
  address: '',
  tax_id: '',
  is_active: true,
}

export function toVendorValues(vendor: Vendor): VendorValues {
  return {
    name: vendor.name,
    contact: vendor.contact ?? '',
    address: vendor.address ?? '',
    tax_id: vendor.tax_id ?? '',
    is_active: vendor.is_active,
  }
}

/**
 * The POST body. `is_active` is left out on purpose: the request model
 * forbids unknown keys, and a new vendor is always active.
 */
export function toCreateBody(values: VendorValues): CreateVendorInput {
  return {
    name: values.name,
    contact: values.contact || null,
    address: values.address || null,
    tax_id: values.tax_id || null,
  }
}

/**
 * The PATCH body: only what changed. An optional field that was emptied goes
 * as `null`, which clears it; `name` and `is_active` are never null — the API
 * answers that with a 422.
 */
export function toUpdateBody(opened: VendorValues, values: VendorValues): UpdateVendorInput {
  const body: UpdateVendorInput = {}
  if (values.name !== opened.name) body.name = values.name
  if (values.contact !== opened.contact) body.contact = values.contact || null
  if (values.address !== opened.address) body.address = values.address || null
  if (values.tax_id !== opened.tax_id) body.tax_id = values.tax_id || null
  if (values.is_active !== opened.is_active) body.is_active = values.is_active
  return body
}
