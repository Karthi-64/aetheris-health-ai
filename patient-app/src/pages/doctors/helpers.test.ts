import { describe, expect, it } from 'vitest'
import { isPublicRef } from '@/api/doctors'
import { readDoctorFilters, withDoctorFilters } from '@/pages/doctors/filters'
import { doctorAvailabilityPath, doctorPath } from '@/pages/doctors/paths'

const REF = 'd0000000-0000-4000-8000-000000000001'
const read = (query: string) => readDoctorFilters(new URLSearchParams(query))

describe('doctor filters in the query string', () => {
  it('reads the search, the department and the page', () => {
    expect(read(`q=rao&dept=${REF}&page=3`)).toEqual({ search: 'rao', department: REF, page: 3 })
    expect(read('')).toEqual({ search: '', department: '', page: 1 })
  })

  it('trims and cuts text to what the API accepts', () => {
    expect(read(`q=${'%20'.repeat(3)}rao%20%20`).search).toBe('rao')
    expect(read(`q=${'a'.repeat(79)}%20b`).search).toBe('a'.repeat(79))
    expect(read(`dept=${'x'.repeat(500)}`).department).toHaveLength(80)
  })

  it.each(['0', '-1', '2.5', 'two', '1001', '', '1e3', ' 2', '02'])('reads a page of %j as the first', (page) => {
    expect(read(`page=${encodeURIComponent(page)}`).page).toBe(1)
  })

  it('writes only what is not at its default, and keeps parameters that are not filters', () => {
    const current = new URLSearchParams(`q=rao&dept=${REF}&page=3&utm=x`)
    expect(withDoctorFilters(current, { page: 1 }).toString()).toBe(`q=rao&dept=${REF}&utm=x`)
    expect(withDoctorFilters(current, { search: '', department: '', page: 1 }).toString()).toBe('utm=x')
    expect(withDoctorFilters(new URLSearchParams(), { search: 'joint care', page: 2 }).toString()).toBe('q=joint+care&page=2')
    // The city of the hospital list is not a filter here.
    expect(read('city=Bengaluru')).toEqual({ search: '', department: '', page: 1 })
  })
})

describe('doctor paths and references', () => {
  it('escapes each reference into one path segment', () => {
    expect(doctorPath('city-care', REF)).toBe(`/hospitals/city-care/doctors/${REF}`)
    expect(doctorAvailabilityPath('city-care', REF)).toBe(`/hospitals/city-care/doctors/${REF}/availability`)
    expect(doctorPath('city-care', '../../me?x#y')).toBe('/hospitals/city-care/doctors/..%2F..%2Fme%3Fx%23y')
    expect(doctorAvailabilityPath('city-care', '../../me?x#y', { date: '2026-10-08' })).toBe(
      '/hospitals/city-care/doctors/..%2F..%2Fme%3Fx%23y/availability?date=2026-10-08',
    )
  })

  it.each([REF, REF.toUpperCase(), '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55'])('takes %s for a reference', (ref) => {
    expect(isPublicRef(ref)).toBe(true)
  })

  it.each(['', 'asha-rao', '42', '.', '..', `${REF}/`, `${REF}\n`, ` ${REF}`, `${REF}?x=1`, REF.replaceAll('-', ''), `{${REF}}`, REF.replace('d', 'g')])(
    'takes %j for no reference at all',
    (ref) => {
      expect(isPublicRef(ref)).toBe(false)
    },
  )
})
