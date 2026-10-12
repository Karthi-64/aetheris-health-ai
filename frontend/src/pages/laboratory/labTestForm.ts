import { z } from 'zod'
import type { CreateLabTestInput, LabTest, ReferenceRange, UpdateLabTestInput } from '@/api/lab'
import { MONEY_PATTERN } from '@/lib/money'

/** A reference bound as typed: empty, or a decimal (the API takes decimal strings). */
const BOUND = /^-?\d+(\.\d+)?$/
const isAge = (v: string) => /^\d+$/.test(v) && Number(v) <= 150

/**
 * One band as typed. It is only checked when the test is numeric (below): a
 * text test's bands are hidden and never sent, so a half-filled one left in
 * the form must not block saving.
 */
const rangeSchema = z.object({
  sex: z.enum(['any', 'male', 'female']),
  age_min: z.string().trim(),
  age_max: z.string().trim(),
  low: z.string().trim(),
  high: z.string().trim(),
  critical_low: z.string().trim(),
  critical_high: z.string().trim(),
})

function checkRange(r: z.infer<typeof rangeSchema>, ctx: z.RefinementCtx, index: number) {
  const at = (field: string) => ['reference_ranges', index, field]
  for (const field of ['age_min', 'age_max'] as const) {
    if (r[field] && !isAge(r[field])) {
      ctx.addIssue({ code: 'custom', path: at(field), message: 'Whole years, 0–150' })
    }
  }
  for (const field of ['low', 'high', 'critical_low', 'critical_high'] as const) {
    if (r[field] && !BOUND.test(r[field])) {
      ctx.addIssue({ code: 'custom', path: at(field), message: 'Enter a number' })
    }
  }
  // The API needs at least one of low / high on every band (§8.3).
  if (!r.low && !r.high) {
    ctx.addIssue({ code: 'custom', path: at('low'), message: 'Give a low or a high bound' })
  }
  if (isAge(r.age_min) && isAge(r.age_max) && Number(r.age_min) > Number(r.age_max)) {
    ctx.addIssue({ code: 'custom', path: at('age_max'), message: 'Must not be below the youngest age' })
  }
  if (BOUND.test(r.low) && BOUND.test(r.high) && Number(r.low) > Number(r.high)) {
    ctx.addIssue({ code: 'custom', path: at('high'), message: 'Must not be below the low bound' })
  }
}

/**
 * The catalog form (docs/18-API_CONTRACTS.md §8.3). Limits mirror the API's:
 * code ≤ 50 of letters, digits, `-` and `_`; name ≤ 200; category ≤ 100;
 * unit ≤ 20; turnaround 1–8760 hours; up to 50 range bands.
 */
export const labTestSchema = z
  .object({
    code: z
      .string()
      .trim()
      .min(1, 'Enter a code')
      .max(50, 'Keep the code under 50 characters')
      .regex(/^[A-Za-z0-9_-]+$/, 'Letters, digits, - and _ only'),
    name: z.string().trim().min(1, 'Enter a name').max(200, 'Keep the name under 200 characters'),
    category: z.string().trim().max(100, 'Keep the category under 100 characters'),
    unit: z.string().trim().max(20, 'Keep the unit under 20 characters'),
    result_type: z.enum(['numeric', 'text']),
    price: z.string().trim().regex(MONEY_PATTERN, 'Enter an amount such as 250.00'),
    turnaround_hours: z
      .string()
      .trim()
      .refine(
        (v) => v === '' || (/^\d+$/.test(v) && Number(v) >= 1 && Number(v) <= 8760),
        'Whole hours, 1–8760',
      ),
    is_active: z.boolean(),
    reference_ranges: z.array(rangeSchema).max(50, 'A test can hold at most 50 ranges'),
  })
  .superRefine((values, ctx) => {
    // A numeric test needs a range to be judged against; a text test has none.
    if (values.result_type !== 'numeric') return
    if (values.reference_ranges.length === 0) {
      ctx.addIssue({
        code: 'custom',
        path: ['reference_ranges'],
        message: 'A numeric test needs at least one reference range',
      })
    }
    values.reference_ranges.forEach((range, index) => checkRange(range, ctx, index))
  })

export type LabTestValues = z.infer<typeof labTestSchema>
export type RangeValues = LabTestValues['reference_ranges'][number]

export const EMPTY_RANGE: RangeValues = {
  sex: 'any',
  age_min: '',
  age_max: '',
  low: '',
  high: '',
  critical_low: '',
  critical_high: '',
}

export const EMPTY_TEST: LabTestValues = {
  code: '',
  name: '',
  category: '',
  unit: '',
  result_type: 'numeric',
  price: '0.00',
  turnaround_hours: '',
  is_active: true,
  reference_ranges: [EMPTY_RANGE],
}

/** Bounds arrive as four-decimal strings; show them as they would be typed. */
const shown = (v: string | null | undefined) =>
  v === null || v === undefined ? '' : v.includes('.') ? v.replace(/0+$/, '').replace(/\.$/, '') : v

export function toLabTestValues(test: LabTest): LabTestValues {
  return {
    code: test.code,
    name: test.name,
    category: test.category ?? '',
    unit: test.unit ?? '',
    result_type: test.result_type,
    price: test.price,
    turnaround_hours: test.turnaround_hours === null ? '' : String(test.turnaround_hours),
    is_active: test.is_active,
    reference_ranges: test.reference_ranges.map((r) => ({
      sex: r.sex,
      age_min: r.age_min === null || r.age_min === undefined ? '' : String(r.age_min),
      age_max: r.age_max === null || r.age_max === undefined ? '' : String(r.age_max),
      low: shown(r.low),
      high: shown(r.high),
      critical_low: shown(r.critical_low),
      critical_high: shown(r.critical_high),
    })),
  }
}

function toRanges(values: LabTestValues): ReferenceRange[] {
  // A text test cannot carry ranges (§8.3), whatever is left in the form.
  if (values.result_type === 'text') return []
  return values.reference_ranges.map((r) => ({
    sex: r.sex,
    age_min: r.age_min === '' ? null : Number(r.age_min),
    age_max: r.age_max === '' ? null : Number(r.age_max),
    low: r.low || null,
    high: r.high || null,
    critical_low: r.critical_low || null,
    critical_high: r.critical_high || null,
  }))
}

export function toCreateBody(values: LabTestValues): CreateLabTestInput {
  return {
    code: values.code.toUpperCase(),
    name: values.name,
    category: values.category || null,
    unit: values.unit || null,
    result_type: values.result_type,
    reference_ranges: toRanges(values),
    turnaround_hours: values.turnaround_hours === '' ? null : Number(values.turnaround_hours),
    price: values.price,
  }
}

/**
 * The PATCH body: only what changed. `code` and `result_type` are never sent —
 * the API rejects them (§8.3) — and the ranges go as a whole list when any of
 * them changed, because that is how the API replaces them.
 */
export function toUpdateBody(opened: LabTestValues, values: LabTestValues): UpdateLabTestInput {
  const body: UpdateLabTestInput = {}
  if (values.name !== opened.name) body.name = values.name
  if (values.category !== opened.category) body.category = values.category || null
  if (values.unit !== opened.unit) body.unit = values.unit || null
  if (values.price !== opened.price) body.price = values.price
  if (values.turnaround_hours !== opened.turnaround_hours) {
    body.turnaround_hours = values.turnaround_hours === '' ? null : Number(values.turnaround_hours)
  }
  if (values.is_active !== opened.is_active) body.is_active = values.is_active
  const before = JSON.stringify(toRanges(opened))
  const after = toRanges(values)
  if (JSON.stringify(after) !== before) body.reference_ranges = after
  return body
}
