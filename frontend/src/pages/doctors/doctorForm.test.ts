import { describe, it, expect } from 'vitest'
import type { Doctor } from '@/api/doctors'
import {
  UNASSIGNED,
  changedKeys,
  doctorSchema,
  toFormValues,
  toRequest,
  type DoctorFormValues,
} from './doctorForm'

/**
 * The rules between the edit form and the `PATCH /doctors/{id}` body
 * (`backend/app/schemas/doctor.py`), without a screen in the way.
 */

const doctor: Doctor = {
  id: 'doc-1',
  hospital_id: 'hosp-1',
  user_id: 'user-9',
  full_name: 'Priya Sharma',
  email: 'priya.sharma@example.com',
  specialization: 'Cardiology',
  license_number: 'MCI-48213',
  consultation_fee: '950.00',
  department_id: 'dep-cardio',
  department_name: 'Cardiology',
  // The API returns both of these shapes for an item with no institution or year.
  qualifications: [{ degree: 'MBBS', institution: null, year: null }, { degree: 'MD' }],
  languages: ['English', 'Hindi'],
  bio: null,
  status: 'active',
  created_at: '2026-01-05T04:00:00Z',
  updated_at: '2026-09-01T04:00:00Z',
}

const opened = toFormValues(doctor)
const edited = (change: Partial<DoctorFormValues>): DoctorFormValues => ({ ...opened, ...change })
const feeErrors = (consultation_fee: string) => {
  const result = doctorSchema.safeParse(edited({ consultation_fee }))
  return result.success ? [] : result.error.issues.map((issue) => issue.message)
}
/** Every complaint the form has about these values, as `path: message`. */
const problems = (values: DoctorFormValues) => {
  const result = doctorSchema.safeParse(values)
  return result.success ? [] : result.error.issues.map((issue) => `${issue.path.join('.')}: ${issue.message}`)
}
const withQualification = (row: Partial<DoctorFormValues['qualifications'][number]>) =>
  edited({ qualifications: [{ degree: 'MD', institution: '', year: '', ...row }] })

describe('the request built from the form', () => {
  it('round-trips an untouched record to no changes at all', () => {
    expect(changedKeys(opened, toFormValues(doctor))).toEqual({})
  })

  it('carries exactly the seven keys the API accepts', () => {
    expect(Object.keys(toRequest(opened)).sort()).toEqual([
      'bio',
      'consultation_fee',
      'department_id',
      'languages',
      'license_number',
      'qualifications',
      'specialization',
    ])
  })

  it.each([
    ['950', '950.00'],
    ['950.5', '950.50'],
    ['0950.50', '950.50'],
    ['0', '0.00'],
    ['000', '0.00'],
    ['0.1', '0.10'],
    [' 1200 ', '1200.00'],
    ['999999.99', '999999.99'],
  ])('writes the fee "%s" as the text "%s"', (typed, sent) => {
    expect(toRequest(edited({ consultation_fee: typed })).consultation_fee).toBe(sent)
  })

  it('does not count a fee retyped in another form as a change', () => {
    expect(changedKeys(opened, edited({ consultation_fee: '950' }))).toEqual({})
    expect(changedKeys(opened, edited({ consultation_fee: '0950.0' }))).toEqual({})
  })

  it('maps "Unassigned" to null and anything else to its id', () => {
    expect(changedKeys(opened, edited({ department_id: UNASSIGNED }))).toEqual({ department_id: null })
    expect(changedKeys(opened, edited({ department_id: 'dep-neuro' }))).toEqual({ department_id: 'dep-neuro' })
  })

  it('does not resend qualifications that only differ in how blanks were stored', () => {
    expect(toRequest(opened).qualifications).toEqual([{ degree: 'MBBS' }, { degree: 'MD' }])
    const retyped = opened.qualifications.map((q) => ({ ...q, institution: '  ' }))
    expect(changedKeys(opened, edited({ qualifications: retyped }))).toEqual({})
  })

  it('treats a bio of only spaces as cleared, which is already the case here', () => {
    expect(toRequest(edited({ bio: '   ' })).bio).toBeNull()
    expect(changedKeys(opened, edited({ bio: '   ' }))).toEqual({})
  })

  it('strips a NUL, which the database would refuse', () => {
    const body = changedKeys(opened, edited({ specialization: 'Card\u0000iology ', bio: 'A\u0000B' }))
    expect(body).toEqual({ bio: 'AB' })
  })
})

describe('the fee the form accepts', () => {
  it.each(['0', '0.5', '950', '950.00', '999999.99', '0999999.99', ' 12.5 '])('accepts "%s"', (fee) => {
    expect(feeErrors(fee)).toEqual([])
  })

  it.each(['', '.5', '5.', '1e2', '1,200', '+5', '-1', '10.123', 'NaN'])(
    'rejects "%s" as not an amount',
    (fee) => {
      expect(feeErrors(fee)).toContain('Enter a fee, with at most 2 decimals')
    },
  )

  it.each(['1000000', '1000000.00', '99999999999999999999'])('rejects "%s" as too much', (fee) => {
    expect(feeErrors(fee)).toEqual(['The fee cannot be more than 999999.99'])
  })
})

describe('the text the form accepts', () => {
  // Blank as the API judges it. Its trim (Python's `strip()`) also takes
  // U+001C to U+001F and U+0085, and a NUL is taken out before anything is sent.
  const BLANK = [
    ['nothing', ''],
    ['spaces', '   '],
    ['a NUL', '\u0000'],
    ['a NUL between spaces', ' \u0000 '],
    ['U+001F', '\u001f'],
    ['U+0085', '\u0085'],
    ['a mix of them', '\u001c\u001d \u001e\u0000'],
  ]

  it.each(BLANK)('rejects a specialization of only %s', (_what, specialization) => {
    expect(problems(edited({ specialization }))).toEqual(['specialization: Enter a specialization'])
  })

  it.each(BLANK)('rejects a licence number of only %s', (_what, license_number) => {
    expect(problems(edited({ license_number }))).toEqual(['license_number: Enter the licence number'])
  })

  it.each(BLANK)('rejects a degree of only %s', (_what, degree) => {
    expect(problems(withQualification({ degree }))).toEqual(['qualifications.0.degree: Enter the degree'])
  })

  it('sends what is left once those characters are taken off the ends', () => {
    const typed = edited({
      specialization: '\u001f Cardio\u0000logy \u0085',
      license_number: '\u001cMCI-1\u001d',
      qualifications: [{ degree: '\u001eMD\u0000', institution: '\u0085AIIMS\u001f', year: '' }],
      bio: '\u001f',
    })
    expect(problems(typed)).toEqual([])
    expect(toRequest(typed)).toMatchObject({
      specialization: 'Cardiology',
      license_number: 'MCI-1',
      qualifications: [{ degree: 'MD', institution: 'AIIMS' }],
      bio: null,
    })
  })

  it.each([
    ['a specialization', edited({ specialization: 'x'.repeat(101) }), 'specialization: Use at most 100 characters'],
    ['a licence number', edited({ license_number: 'x'.repeat(51) }), 'license_number: Use at most 50 characters'],
    ['a bio', edited({ bio: 'x'.repeat(5001) }), 'bio: Use at most 5000 characters'],
    [
      'a degree',
      withQualification({ degree: 'x'.repeat(101) }),
      'qualifications.0.degree: Use at most 100 characters',
    ],
    [
      'an institution',
      withQualification({ institution: 'x'.repeat(201) }),
      'qualifications.0.institution: Use at most 200 characters',
    ],
  ])('rejects %s one character over the limit', (_what, values, problem) => {
    expect(problems(values)).toEqual([problem])
  })

  it('accepts each at its limit, counting only what is sent', () => {
    const typed = edited({
      specialization: `  ${'x'.repeat(100)}\u0000 `,
      license_number: `${'x'.repeat(50)}\u001f`,
      qualifications: [{ degree: ` ${'x'.repeat(100)} `, institution: `${'x'.repeat(200)}\n`, year: '' }],
      bio: `\n${'x'.repeat(5000)}\n`,
    })
    expect(problems(typed)).toEqual([])
    const body = toRequest(typed)
    expect(body.specialization).toHaveLength(100)
    expect(body.license_number).toHaveLength(50)
    expect(body.qualifications[0].degree).toHaveLength(100)
    expect(body.qualifications[0].institution).toHaveLength(200)
    expect(body.bio).toHaveLength(5000)
  })
})

describe('text the API stores exactly as sent', () => {
  // A record written by another client: the API trims neither a bio nor a
  // qualification, so both can be held with padding.
  const padded = toFormValues({
    ...doctor,
    bio: '\n\nText with padding.  ',
    qualifications: [{ degree: ' MD ', institution: ' AIIMS ' }],
  })

  it('sends a bio when only its padding was taken off', () => {
    expect(changedKeys(padded, { ...padded, bio: 'Text with padding.' })).toEqual({
      bio: 'Text with padding.',
    })
  })

  it('sends the qualifications when only the padding of one was taken off', () => {
    const retyped = [{ degree: 'MD', institution: ' AIIMS ', year: '' }]
    expect(changedKeys(padded, { ...padded, qualifications: retyped })).toEqual({
      qualifications: [{ degree: 'MD', institution: 'AIIMS' }],
    })
  })

  it('leaves them out when they were not edited, padded or not', () => {
    expect(changedKeys(padded, padded)).toEqual({})
    expect(changedKeys(padded, { ...padded, consultation_fee: '1200' })).toEqual({
      consultation_fee: '1200.00',
    })
  })

  it('does not count padding typed around text that is already clean', () => {
    const tidy = toFormValues({ ...doctor, bio: 'Text.', qualifications: [{ degree: 'MD' }] })
    const typed = { ...tidy, bio: ' Text.\n', qualifications: [{ degree: ' MD ', institution: ' ', year: '' }] }
    expect(changedKeys(tidy, typed)).toEqual({})
  })

  it('keeps the padding out of what the schema hands on, so an untouched field can be told apart', () => {
    expect(doctorSchema.parse(padded)).toEqual(padded)
  })
})
