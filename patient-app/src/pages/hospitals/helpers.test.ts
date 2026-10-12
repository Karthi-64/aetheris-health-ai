import { ApiError } from '@atheris/api-core'
import { describe, expect, it, vi } from 'vitest'
import { addressLines, locationLine, telHref } from '@/pages/hospitals/address'
import { readFilters, withFilters } from '@/pages/hospitals/filters'
import { hospitalCodeFrom, linkStateFor } from '@/pages/hospitals/linkState'
import { loadFailureOf } from '@/pages/hospitals/loadFailure'
import { hospitalDoctorsPath, hospitalPath } from '@/pages/hospitals/paths'

const filtersOf = (query: string) => readFilters(new URLSearchParams(query))

describe('discovery filters and the query string', () => {
  it('reads the search, the city and the page', () => {
    expect(filtersOf('q=care&city=Bengaluru&page=3')).toEqual({ search: 'care', city: 'Bengaluru', page: 3 })
    expect(filtersOf('')).toEqual({ search: '', city: '', page: 1 })
  })

  it('trims text and cuts it to the 80 characters the API accepts', () => {
    const { search, city } = filtersOf(`q=${'%20'.repeat(3)}${'a'.repeat(79)}%20b&city=%20Mysuru%20`)
    expect(search).toBe('a'.repeat(79))
    expect(city).toBe('Mysuru')
  })

  it.each(['0', '-1', '1.5', '1e3', ' 2', '2 ', '0x10', '१२', '1001', '99999999999999999999', 'NaN', ''])(
    'treats a page of "%s" as the first page',
    (page) => {
      expect(readFilters(new URLSearchParams({ page })).page).toBe(1)
    },
  )

  it('accepts the first and the last page the API does', () => {
    expect(filtersOf('page=1').page).toBe(1)
    expect(filtersOf('page=1000').page).toBe(1000)
  })

  it('writes only what is not at its default, and keeps parameters that are not filters', () => {
    const next = withFilters(new URLSearchParams('q=care&page=4&utm=x'), { search: '', city: 'Mysuru', page: 1 })
    expect(next.toString()).toBe('utm=x&city=Mysuru')
  })

  it('escapes whatever is typed, so text cannot add a parameter of its own', () => {
    const next = withFilters(new URLSearchParams(), { search: 'a&page=9&city=x#y' })
    expect(next.getAll('page')).toEqual([])
    expect(readFilters(new URLSearchParams(next.toString()))).toEqual({ search: 'a&page=9&city=x#y', city: '', page: 1 })
  })
})

describe('address and phone', () => {
  const full = { line1: ' 12 MG Road ', line2: 'Indiranagar', city: 'Bengaluru', state: 'Karnataka', postal_code: '560038', country: 'India' }
  const empty = { line1: null, line2: null, city: null, state: null, postal_code: null, country: null }

  it('builds one line for a card: city and state, else the first address line, else nothing', () => {
    expect(locationLine(full)).toBe('Bengaluru, Karnataka')
    expect(locationLine({ ...full, state: null })).toBe('Bengaluru')
    expect(locationLine({ ...full, city: '  ', state: '' })).toBe('12 MG Road')
    expect(locationLine(empty)).toBe('')
  })

  it('prints only the lines that exist', () => {
    expect(addressLines(full)).toEqual(['12 MG Road', 'Indiranagar', 'Bengaluru, Karnataka 560038', 'India'])
    expect(addressLines({ ...empty, postal_code: '560038' })).toEqual(['560038'])
    expect(addressLines(empty)).toEqual([])
  })

  it('survives an address the contract does not allow, without printing it', () => {
    expect(addressLines(null)).toEqual([])
    expect(locationLine(undefined)).toBe('')
    const odd = { ...empty, city: 42, line1: { text: 'x' } } as unknown as typeof empty
    expect(addressLines(odd)).toEqual([])
  })

  it('makes a tel: link from the digits alone', () => {
    expect(telHref('+91 80 5550 0100')).toBe('tel:+918055500100')
    expect(telHref('(080) 5550-0100')).toBe('tel:08055500100')
    expect(telHref('  +1 415.555.0100 ')).toBe('tel:+14155550100')
  })

  it.each([
    null,
    undefined,
    '',
    '   ',
    'Ask at reception',
    '12',
    '+',
    // Dialling the digits of these would call a different number.
    '+1 (415) 555-0100 ext. 7',
    '080 5550 0100 / 0200',
    'javascript:alert(1)',
    'javascript:alert(1)//+91 80 5550 0100',
    'tel:+918055500100',
    '+91 80 5550 0100\n+91 80 5550 0999',
    '+91 80 5550 0100 080 5550 0999',
  ])('makes no link out of %j', (phone) => {
    expect(telHref(phone)).toBeNull()
  })
})

describe('paths and link state', () => {
  it('escapes a reference into one path segment', () => {
    expect(hospitalPath('city-care')).toBe('/hospitals/city-care')
    expect(hospitalDoctorsPath('city-care')).toBe('/hospitals/city-care/doctors')
    expect(hospitalPath('../../me?x#y')).toBe('/hospitals/..%2F..%2Fme%3Fx%23y')
  })

  it('carries only the hospital code to the link form, and reads back only a usable one', () => {
    expect(linkStateFor('city-care')).toEqual({ hospitalCode: 'city-care' })
    expect(hospitalCodeFrom(linkStateFor('  city-care  '))).toBe('city-care')
    expect(hospitalCodeFrom({ hospitalCode: 'x'.repeat(100) })).toBe('x'.repeat(100))
    for (const state of [null, undefined, 'city-care', 42, [], {}, { hospitalCode: 7 }, { hospitalCode: 'x'.repeat(101) }, { code: 'city-care' }]) {
      expect(hospitalCodeFrom(state)).toBe('')
    }
  })
})

describe('why a hospital screen did not load', () => {
  it.each([
    ['404', new ApiError('x', 'RESOURCE_NOT_FOUND', 404), 'not_found'],
    ['a 404 without the code', new ApiError('x', 'unknown', 404), 'not_found'],
    ['a pending policy', new ApiError('x', 'CONSENT_REQUIRED', 403), 'policies_pending'],
    ['another 403', new ApiError('x', 'FORBIDDEN', 403), 'error'],
    ['the rate limit', new ApiError('x', 'RATE_LIMITED', 429), 'busy'],
    ['a refused parameter', new ApiError('x', 'VALIDATION_ERROR', 422), 'error'],
    ['a server failure', new ApiError('x', 'INTERNAL_ERROR', 500), 'error'],
    // A gateway that answers is not "no connection", whatever its body.
    ['a gateway error with no envelope', new ApiError('x', 'network_error', 502), 'error'],
    ['no answer at all', new ApiError('Network Error', 'network_error'), 'offline'],
    ['a malformed answer', new ApiError('Response is missing pagination metadata', 'bad_response'), 'error'],
    ['something that is not an API error', new TypeError('boom'), 'error'],
  ])('classifies %s', (_case, error, expected) => {
    expect(loadFailureOf(error)).toBe(expected)
  })

  it('believes the browser when it says there is no network — but not over an answer the server gave', () => {
    vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false)

    expect(loadFailureOf(new TypeError('boom'))).toBe('offline')
    expect(loadFailureOf(new ApiError('x', 'bad_response'))).toBe('offline')
    expect(loadFailureOf(new ApiError('x', 'RESOURCE_NOT_FOUND', 404))).toBe('not_found')
    expect(loadFailureOf(new ApiError('x', 'INTERNAL_ERROR', 500))).toBe('error')
  })
})
