import { z } from 'zod'
import type { BloodGroup, Gender } from '@/api/patients'

/**
 * The rules registering and editing a patient have in common. Both dialogs
 * write the same record through the same API validation, so the option lists
 * and field rules are stated once here rather than drifting apart.
 */

export const GENDERS = ['male', 'female', 'other', 'unspecified'] as const satisfies readonly Gender[]

export const BLOOD_GROUPS = [
  'A+',
  'A-',
  'B+',
  'B-',
  'AB+',
  'AB-',
  'O+',
  'O-',
] as const satisfies readonly BloodGroup[]

/** Backend rule: date of birth cannot be in the future, and age cannot exceed 130. */
const MAX_AGE_YEARS = 130

export function isPlausibleBirthDate(value: string): boolean {
  const dob = new Date(value)
  if (Number.isNaN(dob.getTime())) return false
  const today = new Date()
  if (dob > today) return false
  const oldest = new Date()
  oldest.setFullYear(oldest.getFullYear() - MAX_AGE_YEARS)
  return dob >= oldest
}

/**
 * What the backend can normalize to E.164 (`app/utils/phone.py`), written with
 * or without spaces and dashes: a number with its `+` and country code, or a
 * bare Indian mobile, which the API prefixes with +91. It adds a country code
 * to nothing else — a landline, or a foreign number without its `+`, is a 422.
 */
export function isPhoneNumber(value: string): boolean {
  const digits = value.replace(/[\s-]/g, '')
  return /^\+[1-9]\d{6,14}$/.test(digits) || /^0?[6-9]\d{9}$/.test(digits)
}

export const INVALID_PHONE =
  'Enter a 10-digit mobile number, or include the country code, e.g. +91 98123 45678'

/** The backend's own pattern: one `@`, no spaces, and a dotted domain with no empty label. */
const EMAIL_PATTERN = /^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$/

const nameField = (required: string) =>
  z.string().trim().min(1, required).max(100, 'Use at most 100 characters')

export const firstNameField = nameField('First name is required')

export const lastNameField = nameField('Last name is required')

export const birthDateField = z
  .string()
  .min(1, 'Date of birth is required')
  .refine(isPlausibleBirthDate, 'Enter a date in the past, within the last 130 years')

export const genderField = z.enum(GENDERS, { message: 'Select a gender' })

// Optional, but must be E.164-able if given — the backend normalizes
// `9876543210` to `+919876543210` and rejects anything it cannot parse.
export const phoneField = z
  .string()
  .trim()
  .refine((v) => v === '' || isPhoneNumber(v), INVALID_PHONE)

export const emailField = z
  .string()
  .trim()
  .refine((v) => v === '' || EMAIL_PATTERN.test(v), 'Enter a valid email')
