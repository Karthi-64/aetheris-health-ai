import { z } from 'zod'
import type { CreatePrescriptionInput, PrescriptionLineInput } from '@/api/pharmacy'

/** The API takes 1–50 lines per prescription (docs/18-API_CONTRACTS.md §9.4). */
export const MAX_LINES = 50

/** Where a line's medicine comes from: the catalog, or a name typed for something not stocked. */
export type LineSource = 'catalog' | 'free_text'

const WHOLE = /^\d+$/
const wholeBetween = (value: string, min: number, max: number) =>
  WHOLE.test(value) && Number(value) >= min && Number(value) <= max

/**
 * One line as typed. Nothing is checked here: which of `medicine_id` and
 * `medicine_name` matters depends on `source`, and the one the line does not
 * use is hidden and never sent, so it must not be able to block the form.
 */
const lineSchema = z.object({
  source: z.enum(['catalog', 'free_text']),
  medicine_id: z.string(),
  medicine_name: z.string().trim(),
  dosage: z.string().trim(),
  frequency: z.string().trim(),
  duration_days: z.string().trim(),
  instructions: z.string().trim(),
  quantity: z.string().trim(),
})

type Line = z.infer<typeof lineSchema>

/**
 * Limits mirror the API's (`PrescriptionLineRequest`): name ≤ 200; dosage and
 * frequency 1–100; duration 1–3650 whole days; instructions ≤ 1000; quantity
 * 1–1,000,000 whole units.
 */
function checkLine(line: Line, ctx: z.RefinementCtx, index: number) {
  const issue = (field: keyof Line, message: string) =>
    ctx.addIssue({ code: 'custom', path: ['items', index, field], message })

  if (line.source === 'catalog') {
    if (!line.medicine_id) issue('medicine_id', 'Choose a medicine')
  } else if (!line.medicine_name) {
    issue('medicine_name', "Enter the medicine's name")
  } else if (line.medicine_name.length > 200) {
    issue('medicine_name', 'Keep the name under 200 characters')
  }

  if (!line.dosage) issue('dosage', 'Enter the dosage')
  else if (line.dosage.length > 100) issue('dosage', 'Keep the dosage under 100 characters')

  if (!line.frequency) issue('frequency', 'Enter how often it is taken')
  else if (line.frequency.length > 100) issue('frequency', 'Keep the frequency under 100 characters')

  if (line.duration_days && !wholeBetween(line.duration_days, 1, 3650)) {
    issue('duration_days', 'Whole days, 1–3650')
  }
  if (line.instructions.length > 1000) {
    issue('instructions', 'Keep the instructions under 1,000 characters')
  }
  if (!line.quantity) issue('quantity', 'Enter the quantity')
  else if (!wholeBetween(line.quantity, 1, 1_000_000)) issue('quantity', 'Whole units, 1–1,000,000')
}

/**
 * The prescription form (§9.4). Field names are the API's own, so a 422 that
 * names `items.0.quantity` lands under that field.
 */
export const prescribeSchema = z
  .object({
    notes: z.string().trim().max(2000, 'Keep the notes under 2,000 characters'),
    items: z
      .array(lineSchema)
      .min(1, 'Add at least one medicine')
      .max(MAX_LINES, `A prescription can hold at most ${MAX_LINES} medicines`),
  })
  .superRefine((values, ctx) => {
    values.items.forEach((line, index) => checkLine(line, ctx, index))

    // A catalog medicine may appear once (§9.4); typed names are not compared.
    const seen = new Set<string>()
    values.items.forEach((line, index) => {
      if (line.source !== 'catalog' || !line.medicine_id) return
      if (seen.has(line.medicine_id)) {
        ctx.addIssue({
          code: 'custom',
          path: ['items', index, 'medicine_id'],
          message: 'This medicine is already on the prescription',
        })
      }
      seen.add(line.medicine_id)
    })
  })

export type PrescribeValues = z.infer<typeof prescribeSchema>
export type PrescribeLineValues = PrescribeValues['items'][number]

export function emptyLine(source: LineSource): PrescribeLineValues {
  return {
    source,
    medicine_id: '',
    medicine_name: '',
    dosage: '',
    frequency: '',
    duration_days: '',
    instructions: '',
    quantity: '',
  }
}

/**
 * Exactly one of `medicine_id` and `medicine_name` goes out — both, or
 * neither, is a 422 (§9.4) — whatever the other field still holds from before
 * the line's source was switched.
 */
function toLine(line: PrescribeLineValues): PrescriptionLineInput {
  return {
    ...(line.source === 'catalog' ? { medicine_id: line.medicine_id } : { medicine_name: line.medicine_name }),
    dosage: line.dosage,
    frequency: line.frequency,
    ...(line.duration_days ? { duration_days: Number(line.duration_days) } : {}),
    ...(line.instructions ? { instructions: line.instructions } : {}),
    quantity: Number(line.quantity),
  }
}

/**
 * The body for `POST /prescriptions`. Only the visit is named: the patient and
 * the prescriber are read from it by the server, and sending either is a 422.
 */
export function toPrescriptionBody(appointmentId: string, values: PrescribeValues): CreatePrescriptionInput {
  return {
    appointment_id: appointmentId,
    ...(values.notes ? { notes: values.notes } : {}),
    items: values.items.map(toLine),
  }
}
