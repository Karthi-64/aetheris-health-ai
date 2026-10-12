import { z } from 'zod'
import type { Doctor, Qualification, UpdateDoctorInput } from '@/api/doctors'
import { MONEY_PATTERN } from '@/lib/money'

/** `department_id` value for "no department". The API takes `null`, which a select cannot hold. */
export const UNASSIGNED = 'unassigned'

/** The API's ceiling on a fee. Anything above it is a misplaced decimal point, not a fee. */
const MAX_FEE = '999999.99'

/** The whole part of an amount, without leading zeros. */
const wholeDigits = (amount: string) => amount.split('.')[0].replace(/^0+(?=\d)/, '')

/** At most two decimals is already checked, so six whole digits is the same as "at most MAX_FEE". */
const withinMaxFee = (amount: string) => wholeDigits(amount).length <= wholeDigits(MAX_FEE).length

const isYearOrBlank = (year: string) =>
  year === '' || (/^\d{4}$/.test(year) && Number(year) >= 1900 && Number(year) <= 2100)

/**
 * What the API's blank check (Python's `strip()`) counts as whitespace and
 * `trim()` does not.
 */
const SERVER_ONLY_SPACE = '\u001c\u001d\u001e\u001f\u0085'

const isSpace = (char: string) => char.trim() === '' || SERVER_ONLY_SPACE.includes(char)

/**
 * Text as it is sent. A NUL passes the API's validation but the database
 * refuses it, which comes back as a 500. The ends are trimmed the way the API
 * trims them, so a value it would call blank is blank here too.
 */
function clean(text: string): string {
  const kept = text.replaceAll('\u0000', '')
  let start = 0
  let end = kept.length
  while (start < end && isSpace(kept[start])) start += 1
  while (end > start && isSpace(kept[end - 1])) end -= 1
  return kept.slice(start, end)
}

/**
 * The rules for free text judge the value that would be sent, not the one
 * typed: a NUL or a stray separator must not count towards "not blank". They
 * leave the value itself as typed, which {@link changedKeys} relies on.
 */
const filled = (typed: string) => clean(typed) !== ''
const atMost = (max: number) => (typed: string) => clean(typed).length <= max

const qualificationSchema = z.object({
  degree: z
    .string()
    .refine(filled, 'Enter the degree')
    .refine(atMost(100), 'Use at most 100 characters'),
  institution: z.string().refine(atMost(200), 'Use at most 200 characters'),
  year: z.string().trim().refine(isYearOrBlank, 'Enter a year between 1900 and 2100'),
})

/**
 * The edit form. Its keys and nesting are those of the `PATCH /doctors/{id}`
 * body, so a 422 naming `department_id` or `qualifications.0.degree` lands
 * under that input. The values are what the inputs hold — text throughout —
 * and {@link toRequest} turns them into what the API takes.
 */
export const doctorSchema = z.object({
  specialization: z
    .string()
    .refine(filled, 'Enter a specialization')
    .refine(atMost(100), 'Use at most 100 characters'),
  license_number: z
    .string()
    .refine(filled, 'Enter the licence number')
    .refine(atMost(50), 'Use at most 50 characters'),
  consultation_fee: z
    .string()
    .trim()
    .regex(MONEY_PATTERN, 'Enter a fee, with at most 2 decimals')
    .refine(withinMaxFee, `The fee cannot be more than ${MAX_FEE}`),
  /** A department id, or {@link UNASSIGNED}. */
  department_id: z.string(),
  qualifications: z.array(qualificationSchema),
  /** Comma-separated. */
  languages: z.string(),
  bio: z.string().refine(atMost(5000), 'Use at most 5000 characters'),
})

export type DoctorFormValues = z.infer<typeof doctorSchema>
export type QualificationValues = DoctorFormValues['qualifications'][number]

export const EMPTY_QUALIFICATION: QualificationValues = { degree: '', institution: '', year: '' }

/** Load a doctor's record into the form. */
export function toFormValues(doctor: Doctor): DoctorFormValues {
  return {
    specialization: doctor.specialization,
    license_number: doctor.license_number,
    consultation_fee: doctor.consultation_fee,
    department_id: doctor.department_id ?? UNASSIGNED,
    qualifications: doctor.qualifications.map((q) => ({
      degree: q.degree,
      institution: q.institution ?? '',
      year: q.year == null ? '' : String(q.year),
    })),
    languages: doctor.languages.join(', '),
    bio: doctor.bio ?? '',
  }
}

/**
 * An amount the way the API returns it — "950" becomes "950.00" — so a fee
 * retyped without changing it compares equal. Text in, text out: an amount
 * that is sent never passes through a float.
 */
function canonicalFee(amount: string): string {
  const decimals = amount.split('.')[1] ?? ''
  return `${wholeDigits(amount)}.${decimals.padEnd(2, '0')}`
}

/** Optional keys are left out when blank; an item must not carry any other key. */
function toQualification(row: QualificationValues, freeText: (text: string) => string): Qualification {
  const institution = freeText(row.institution)
  const year = clean(row.year)
  return {
    degree: freeText(row.degree),
    ...(institution ? { institution } : {}),
    ...(year ? { year: Number(year) } : {}),
  }
}

/**
 * Every editable key as the API takes it.
 *
 * `freeText` is applied to the text the API keeps exactly as sent: a bio, and
 * a qualification's degree and institution. It is cleaned like everything
 * else, unless {@link changedKeys} asks for it as it stands.
 */
export function toRequest(
  values: DoctorFormValues,
  freeText: (text: string) => string = clean,
): Required<UpdateDoctorInput> {
  return {
    specialization: clean(values.specialization),
    license_number: clean(values.license_number),
    consultation_fee: canonicalFee(clean(values.consultation_fee)),
    department_id: values.department_id === UNASSIGNED ? null : values.department_id,
    qualifications: values.qualifications.map((row) => toQualification(row, freeText)),
    languages: values.languages.split(',').map(clean).filter(Boolean),
    // An empty string would be stored as one; null is how a bio is cleared.
    bio: freeText(values.bio) || null,
  }
}

const asItStands = (text: string) => text

const differ = (a: unknown, b: unknown) => JSON.stringify(a) !== JSON.stringify(b)

/**
 * The PATCH body: only the keys whose value differs from what the form was
 * opened with. An unchanged key must stay out — the API re-checks whatever it
 * is sent, so resending a department that has since been deactivated fails
 * the whole save. Compared as serialized request values rather than by dirty
 * flags, so a list edited back to where it started is not resent.
 *
 * A key goes in when it was edited and what would be sent is not what the
 * record holds. Both halves matter for the text the API keeps as sent, which
 * a record written by another client can hold with padding: taking the
 * padding off is a change although both sides clean to the same text, and a
 * field left alone is not one although cleaning it would alter it.
 *
 * Can be empty, which the API rejects: the caller must not send that.
 */
export function changedKeys(opened: DoctorFormValues, current: DoctorFormValues): UpdateDoctorInput {
  const held = toRequest(opened, asItStands)
  const typed = toRequest(current, asItStands)
  const body = toRequest(current)
  const keys = Object.keys(body) as Array<keyof UpdateDoctorInput>
  return Object.fromEntries(
    keys
      .filter((key) => differ(typed[key], held[key]) && differ(body[key], held[key]))
      .map((key) => [key, body[key]]),
  )
}
