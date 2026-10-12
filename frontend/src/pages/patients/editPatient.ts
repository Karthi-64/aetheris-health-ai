import { z } from 'zod'
import type {
  AllergySeverity,
  BloodGroup,
  EmergencyContact,
  Patient,
  PatientAddress,
  UpdatePatientInput,
} from '@/api/patients'
import {
  BLOOD_GROUPS,
  INVALID_PHONE,
  birthDateField,
  emailField,
  firstNameField,
  genderField,
  isPhoneNumber,
  lastNameField,
  phoneField,
} from './patientForm'

/**
 * The edit form's values, rules and translation to `PATCH /patients/{id}`.
 *
 * The values are shaped like the request body — `address.line1`,
 * `emergency_contact.phone`, `allergies[i].name` — so a 422 naming one of
 * those paths lands under the input that caused it. They differ from the body
 * in one way: every input holds text, so "nothing here" is `''` in the form
 * and `null`, or an absent key, in the request.
 */

/** Stands for "no blood group on record" in the select, whose items cannot have an empty value. */
export const NOT_RECORDED = 'not_recorded'

export const SEVERITIES = ['mild', 'moderate', 'severe'] as const satisfies readonly AllergySeverity[]

const atMost = (max: number) => `Use at most ${max} characters`
const text = (max: number) => z.string().trim().max(max, atMost(max))
const rowName = (message: string) => z.string().trim().min(1, message).max(200, atMost(200))

/** A date input yields `''` or `YYYY-MM-DD`; the API must be sent nothing else. */
const dateField = z
  .string()
  .refine((v) => v === '' || /^\d{4}-\d{2}-\d{2}$/.test(v), 'Enter a valid date')

function isOnsetYear(value: string): boolean {
  if (value === '') return true
  if (!/^\d{4}$/.test(value)) return false
  // The API compares with the current year in UTC.
  return Number(value) >= 1900 && Number(value) <= new Date().getUTCFullYear()
}

const isBlank = (group: Record<string, string>) => Object.values(group).every((v) => v.trim() === '')

/**
 * An address is stored whole or not at all: all blank removes it, anything
 * else needs the three parts the API requires.
 */
const addressSchema = z
  .object({
    line1: text(200),
    line2: text(200),
    city: text(100),
    state: text(100),
    postal_code: text(20),
    country: z.string().trim(),
  })
  .superRefine((address, ctx) => {
    if (isBlank(address)) return
    if (!address.line1) {
      ctx.addIssue({ code: 'custom', path: ['line1'], message: 'An address needs its first line' })
    }
    if (!address.city) {
      ctx.addIssue({ code: 'custom', path: ['city'], message: 'An address needs a city' })
    }
    if (!/^[A-Za-z]{2}$/.test(address.country)) {
      ctx.addIssue({
        code: 'custom',
        path: ['country'],
        message: 'Enter the two-letter country code, e.g. IN',
      })
    }
  })

/** Same all-or-nothing rule: a contact with no name or no reachable number is refused by the API. */
const emergencyContactSchema = z
  .object({ name: text(200), phone: z.string().trim(), relation: text(50) })
  .superRefine((contact, ctx) => {
    if (isBlank(contact)) return
    if (!contact.name) {
      ctx.addIssue({ code: 'custom', path: ['name'], message: "Enter the contact's name" })
    }
    if (!isPhoneNumber(contact.phone)) {
      ctx.addIssue({
        code: 'custom',
        path: ['phone'],
        message: contact.phone ? INVALID_PHONE : "Enter the contact's phone number",
      })
    }
    if (!contact.relation) {
      ctx.addIssue({
        code: 'custom',
        path: ['relation'],
        message: 'Say how the contact is related to the patient',
      })
    }
  })

const allergySchema = z.object({
  name: rowName('Name the allergen, or remove this row'),
  severity: z.enum(SEVERITIES),
  reaction: text(500),
  noted_on: dateField,
})

const conditionSchema = z.object({
  name: rowName('Name the condition, or remove this row'),
  since_year: z.string().trim().refine(isOnsetYear, 'Enter a year from 1900 to this year'),
  notes: text(1000),
})

const medicationSchema = z.object({
  name: rowName('Name the medication, or remove this row'),
  dosage: text(100),
  frequency: text(100),
  started_on: dateField,
})

export const editPatientSchema = z.object({
  first_name: firstNameField,
  last_name: lastNameField,
  date_of_birth: birthDateField,
  gender: genderField,
  blood_group: z.enum([...BLOOD_GROUPS, NOT_RECORDED]),
  phone: phoneField,
  email: emailField.max(200, atMost(200)),
  marital_status: text(20),
  occupation: text(100),
  address: addressSchema,
  emergency_contact: emergencyContactSchema,
  allergies: z.array(allergySchema),
  chronic_conditions: z.array(conditionSchema),
  current_medications: z.array(medicationSchema),
  notes: text(5000),
})

export type EditPatientValues = z.infer<typeof editPatientSchema>

export const EMPTY_ALLERGY: EditPatientValues['allergies'][number] = {
  name: '',
  severity: 'moderate',
  reaction: '',
  noted_on: '',
}

export const EMPTY_CONDITION: EditPatientValues['chronic_conditions'][number] = {
  name: '',
  since_year: '',
  notes: '',
}

export const EMPTY_MEDICATION: EditPatientValues['current_medications'][number] = {
  name: '',
  dosage: '',
  frequency: '',
  started_on: '',
}

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

const isBloodGroup = (value: unknown): value is BloodGroup =>
  BLOOD_GROUPS.some((group) => group === value)

const isSeverity = (value: unknown): value is AllergySeverity =>
  SEVERITIES.some((severity) => severity === value)

/**
 * Fill the form from a record. The API returns the nested objects and list
 * items loosely typed, holding only the keys that were stored, so every
 * optional key is read as possibly absent.
 */
export function toFormValues(patient: Patient): EditPatientValues {
  const address = patient.address ?? {}
  const contact = patient.emergency_contact ?? {}
  return {
    first_name: patient.first_name,
    last_name: patient.last_name,
    date_of_birth: patient.date_of_birth,
    gender: patient.gender,
    blood_group: isBloodGroup(patient.blood_group) ? patient.blood_group : NOT_RECORDED,
    phone: patient.phone ?? '',
    email: patient.email ?? '',
    marital_status: patient.marital_status ?? '',
    occupation: patient.occupation ?? '',
    address: {
      line1: str(address.line1),
      line2: str(address.line2),
      city: str(address.city),
      state: str(address.state),
      postal_code: str(address.postal_code),
      country: str(address.country),
    },
    emergency_contact: {
      name: str(contact.name),
      phone: str(contact.phone),
      relation: str(contact.relation),
    },
    allergies: patient.allergies.map((allergy) => ({
      name: str(allergy.name),
      // An allergy saved without a severity has none stored; the API's default is `moderate`.
      severity: isSeverity(allergy.severity) ? allergy.severity : 'moderate',
      reaction: str(allergy.reaction),
      noted_on: str(allergy.noted_on),
    })),
    chronic_conditions: patient.chronic_conditions.map((condition) => ({
      name: str(condition.name),
      since_year: typeof condition.since_year === 'number' ? String(condition.since_year) : '',
      notes: str(condition.notes),
    })),
    current_medications: patient.current_medications.map((medication) => ({
      name: str(medication.name),
      dosage: str(medication.dosage),
      frequency: str(medication.frequency),
      started_on: str(medication.started_on),
    })),
    notes: patient.notes ?? '',
  }
}

function trimmed<T extends Record<string, string>>(group: T): T {
  return Object.fromEntries(Object.entries(group).map(([key, value]) => [key, value.trim()])) as T
}

const orNull = (value: string): string | null => value.trim() || null

function toAddress(values: EditPatientValues['address']): PatientAddress | null {
  if (isBlank(values)) return null
  const address = trimmed(values)
  return {
    line1: address.line1,
    ...(address.line2 ? { line2: address.line2 } : {}),
    city: address.city,
    ...(address.state ? { state: address.state } : {}),
    ...(address.postal_code ? { postal_code: address.postal_code } : {}),
    country: address.country.toUpperCase(),
  }
}

function toEmergencyContact(values: EditPatientValues['emergency_contact']): EmergencyContact | null {
  return isBlank(values) ? null : trimmed(values)
}

/**
 * Every editable field as the API would be sent it. A blank optional is
 * `null` where the API clears on `null`, and left out of a nested object or
 * list item, which the API stores key for key.
 */
function toRequest(values: EditPatientValues): Required<UpdatePatientInput> {
  return {
    first_name: values.first_name.trim(),
    last_name: values.last_name.trim(),
    date_of_birth: values.date_of_birth,
    gender: values.gender,
    blood_group: values.blood_group === NOT_RECORDED ? null : values.blood_group,
    phone: orNull(values.phone),
    email: orNull(values.email),
    address: toAddress(values.address),
    emergency_contact: toEmergencyContact(values.emergency_contact),
    marital_status: orNull(values.marital_status),
    occupation: orNull(values.occupation),
    allergies: values.allergies.map(trimmed).map((allergy) => ({
      name: allergy.name,
      severity: allergy.severity,
      ...(allergy.reaction ? { reaction: allergy.reaction } : {}),
      ...(allergy.noted_on ? { noted_on: allergy.noted_on } : {}),
    })),
    chronic_conditions: values.chronic_conditions.map(trimmed).map((condition) => ({
      name: condition.name,
      ...(condition.since_year ? { since_year: Number(condition.since_year) } : {}),
      ...(condition.notes ? { notes: condition.notes } : {}),
    })),
    current_medications: values.current_medications.map(trimmed).map((medication) => ({
      name: medication.name,
      ...(medication.dosage ? { dosage: medication.dosage } : {}),
      ...(medication.frequency ? { frequency: medication.frequency } : {}),
      ...(medication.started_on ? { started_on: medication.started_on } : {}),
    })),
    notes: orNull(values.notes),
  }
}

const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

/**
 * The PATCH body: only the top-level keys whose value differs from what the
 * form was opened with. The API rejects an empty body and replaces a nested
 * object or list whole, so an untouched one is left out and a touched one is
 * sent complete. May be empty — the caller must not send it then.
 *
 * Both sides are compared as the request would carry them, not by the form's
 * dirty flags: retyping a value, or adding a row and removing it again, is not
 * a change.
 */
export function changedFields(
  opened: EditPatientValues,
  submitted: EditPatientValues,
): UpdatePatientInput {
  const before = toRequest(opened)
  const after = toRequest(submitted)
  const changed = (Object.keys(after) as (keyof typeof after)[]).filter(
    (key) => !same(before[key], after[key]),
  )
  return Object.fromEntries(changed.map((key) => [key, after[key]])) as UpdatePatientInput
}

/** The parts of the record the API replaces whole, each with the name a user knows it by. */
const REPLACED_WHOLE = {
  address: 'address',
  emergency_contact: 'emergency contact',
  allergies: 'allergies',
  chronic_conditions: 'chronic conditions',
  current_medications: 'current medications',
} as const satisfies Partial<Record<keyof UpdatePatientInput, string>>

type ReplacedWhole = keyof typeof REPLACED_WHOLE

const replacedWhole = (body: UpdatePatientInput) =>
  (Object.keys(REPLACED_WHOLE) as ReplacedWhole[]).filter((key) => key in body)

/** True when `body` carries a part the API replaces whole — see {@link overwrittenParts}. */
export const replacesWholeParts = (body: UpdatePatientInput) => replacedWhole(body).length > 0

/**
 * The parts of `body` that would undo somebody else's edit, by name.
 *
 * A list or nested object is sent complete, built from what the form was
 * opened with. If the stored one has changed since, saving would silently drop
 * that change — an allergy a colleague added — and the API has no version
 * check to refuse it. `current` must therefore be the record as stored now,
 * not the copy the page has been showing.
 *
 * A stored part that already equals what is being sent is not a conflict: that
 * is this user's own save, retried after its answer was lost.
 */
export function overwrittenParts(
  body: UpdatePatientInput,
  opened: EditPatientValues,
  current: Patient,
): string[] {
  const before = toRequest(opened)
  const stored = toRequest(toFormValues(current))
  return replacedWhole(body)
    .filter((key) => !same(stored[key], before[key]) && !same(stored[key], body[key]))
    .map((key) => REPLACED_WHOLE[key])
}
