import { describe, it, expect } from 'vitest'
import { emailField, isPhoneNumber, phoneField } from './patientForm'

/**
 * The shared phone and email rules against what the API accepts on both
 * `POST /patients` and `PATCH /patients/{id}`. Every case below was run through
 * the backend's own validators (`_validate_optional_phone` and
 * `_validate_optional_email` in backend/app/schemas/patient.py), and the
 * verdict here is the backend's.
 */

describe('isPhoneNumber', () => {
  it.each([
    '+919812345678',
    '+91 98123 45678',
    '+91-98123-45678',
    '+ 91 98123 45678',
    '+1 415-555-2671',
    // E.164 at its shortest and longest: 7 and 15 digits.
    '+1234567',
    '+123456789012345',
    // A bare Indian mobile, which the API prefixes with +91 — with or without the trunk 0.
    '9812345678',
    '98123 45678',
    '09812345678',
    '098123-45678',
  ])('accepts %s, which the API stores as E.164', (value) => {
    expect(isPhoneNumber(value)).toBe(true)
  })

  it.each([
    // Landlines, and numbers from elsewhere, without a country code.
    '040 2345 6789',
    '0484 2345678',
    '022-12345678',
    '415 555 2671',
    '12345678',
    // A country code without its +.
    '91 98123 45678',
    // Ten digits, but not an Indian mobile (those start 6-9).
    '5812345678',
    // Not E.164: a leading zero, too short, too long, a second +.
    '+0123456789',
    '+123456',
    '+1234567890123456',
    '+91+9812345678',
    '-------',
    'ask at the desk',
  ])('rejects %s, which the API answers with a 422', (value) => {
    expect(isPhoneNumber(value)).toBe(false)
  })

  it('leaves a blank phone to the field, where it means "none"', () => {
    expect(phoneField.safeParse('  ').success).toBe(true)
    expect(phoneField.safeParse('040 2345 6789').success).toBe(false)
  })
})

describe('emailField', () => {
  it.each(['thomas.george@example.com', 'a@b.c', 'a.b@c.d.e', '  Thomas@Example.com  ', ''])(
    'accepts "%s"',
    (value) => {
      expect(emailField.safeParse(value).success).toBe(true)
    },
  )

  it.each([
    'thomas-at-example',
    'thomas@example',
    'thomas@@example.com',
    'thomas george@example.com',
    // Every label of the domain must hold something.
    'thomas@example..com',
    'thomas@example.com.',
    'thomas@.example.com',
  ])('rejects "%s", which the API answers with a 422', (value) => {
    expect(emailField.safeParse(value).success).toBe(false)
  })
})
