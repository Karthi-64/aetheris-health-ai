import { z } from 'zod'
import type {
  CreateInventoryLocationInput,
  InventoryLocation,
  UpdateInventoryLocationInput,
} from '@/api/inventory'

/**
 * What the API takes as a location code: letters, digits and `- _ . /`,
 * starting with a letter or digit (`_CODE_PATTERN` in
 * `backend/app/schemas/inventory.py`). The server trims and uppercases the
 * value before checking it, so lower case is accepted here — but ASCII only:
 * it measures the 50-character limit before uppercasing, and a letter that
 * grows when uppercased can pass that check and then overflow the column.
 */
const LOCATION_CODE = /^[A-Za-z0-9][A-Za-z0-9_./-]*$/

/**
 * Every field but the code (docs/18-API_CONTRACTS.md §10.3). The name is
 * trimmed before its length is checked: the API measures the raw text, so
 * what is sent has to be the trimmed value.
 */
const fields = {
  name: z.string().trim().min(1, "Enter the location's name").max(200, 'Keep the name to 200 characters'),
  kind: z.enum(['store', 'ward', 'icu', 'ot'], 'Choose a kind'),
  /** Edit only: `POST /inventory/locations` rejects the key, so a new location never sends it. */
  is_active: z.boolean(),
}

/** The form for a new location. */
export const createLocationSchema = z.object({
  name: fields.name,
  code: z
    .string()
    .trim()
    .min(1, 'Enter a code')
    .max(50, 'Keep the code to 50 characters')
    .regex(LOCATION_CODE, 'Letters, digits and - _ . / only, starting with a letter or digit'),
  kind: fields.kind,
  is_active: fields.is_active,
})

/**
 * The form for an existing location. Its code is shown but can never be sent
 * (§10.3), so it is not checked: a field the user cannot change must not block
 * saving the ones they can.
 */
export const editLocationSchema = z.object({
  name: fields.name,
  code: z.string(),
  kind: fields.kind,
  is_active: fields.is_active,
})

export type LocationValues = z.infer<typeof createLocationSchema>

/** A new location is a store unless said otherwise — the API's own default. */
export const EMPTY_LOCATION: LocationValues = {
  name: '',
  code: '',
  kind: 'store',
  is_active: true,
}

export function toLocationValues(location: InventoryLocation): LocationValues {
  return {
    name: location.name,
    code: location.code,
    kind: location.kind,
    is_active: location.is_active,
  }
}

/**
 * The POST body. `is_active` is left out on purpose: the request model
 * forbids unknown keys, and a new location is always active.
 */
export function toCreateBody(values: LocationValues): CreateInventoryLocationInput {
  return {
    name: values.name,
    code: values.code.toUpperCase(),
    kind: values.kind,
  }
}

/**
 * The PATCH body: only what changed. `code` is never sent — the API answers
 * 422 to it (§10.3) — and no field is ever null, which it refuses too.
 */
export function toUpdateBody(opened: LocationValues, values: LocationValues): UpdateInventoryLocationInput {
  const body: UpdateInventoryLocationInput = {}
  if (values.name !== opened.name) body.name = values.name
  if (values.kind !== opened.kind) body.kind = values.kind
  if (values.is_active !== opened.is_active) body.is_active = values.is_active
  return body
}
