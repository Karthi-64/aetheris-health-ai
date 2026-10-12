import { z } from 'zod'
import type { InvoiceItem, InvoiceLineInput, Service } from '@/api/billing'
import { MONEY_PATTERN, isPositiveMoney } from './billing'

/** `service_id` value for a line that is not from the catalog. */
export const CUSTOM_ITEM = 'custom'

/**
 * One editable line. A line is a catalog service *or* a custom item
 * (docs/18-API_CONTRACTS.md §6.4); the description, price and tax flag below
 * are only sent for a custom item.
 */
export const lineSchema = z
  .object({
    service_id: z.string(),
    description: z.string().trim(),
    quantity: z.string().trim(),
    unit_price: z.string().trim(),
    taxable: z.boolean(),
  })
  .superRefine((line, ctx) => {
    if (!line.service_id) {
      ctx.addIssue({ code: 'custom', path: ['service_id'], message: 'Choose a service or a custom item' })
    }
    if (!isPositiveMoney(line.quantity)) {
      ctx.addIssue({
        code: 'custom',
        path: ['quantity'],
        message: 'Enter a quantity above 0, with at most 2 decimals',
      })
    }
    if (line.service_id === CUSTOM_ITEM) {
      if (!line.description) {
        ctx.addIssue({ code: 'custom', path: ['description'], message: 'Describe the item' })
      }
      if (!MONEY_PATTERN.test(line.unit_price)) {
        ctx.addIssue({
          code: 'custom',
          path: ['unit_price'],
          message: 'Enter a price, with at most 2 decimals',
        })
      }
    }
  })

export type LineValues = z.infer<typeof lineSchema>

/** The part of a form that {@link InvoiceLinesFields} edits. */
export interface LinesFormValues {
  items: LineValues[]
}

export const EMPTY_LINE: LineValues = {
  service_id: '',
  description: '',
  quantity: '1',
  unit_price: '',
  taxable: false,
}

/** Build the request line. Catalog lines must not carry a price or tax flag — the API rejects them. */
export function toLineInput(line: LineValues): InvoiceLineInput {
  if (line.service_id === CUSTOM_ITEM) {
    return {
      description: line.description,
      quantity: line.quantity,
      unit_price: line.unit_price,
      taxable: line.taxable,
    }
  }
  return {
    service_id: line.service_id,
    quantity: line.quantity,
    ...(line.description ? { description: line.description } : {}),
  }
}

/**
 * Load an existing line for editing. `PATCH` replaces the whole line set, so
 * each line has to round-trip: a catalog line keeps a description only where
 * it overrides the service's name, and a custom line is taxable when the
 * server recorded a tax rate on it.
 */
export function fromInvoiceItem(item: InvoiceItem, services: Service[]): LineValues {
  if (item.service_id) {
    const service = services.find((s) => s.id === item.service_id)
    return {
      service_id: item.service_id,
      description: service?.name === item.description ? '' : item.description,
      quantity: item.quantity,
      unit_price: '',
      taxable: false,
    }
  }
  return {
    service_id: CUSTOM_ITEM,
    description: item.description,
    quantity: item.quantity,
    unit_price: item.unit_price,
    taxable: /[1-9]/.test(item.tax_rate),
  }
}
