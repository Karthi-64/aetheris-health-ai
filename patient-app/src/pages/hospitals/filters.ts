import { HOSPITAL_FILTER_MAX_LENGTH, HOSPITALS_MAX_PAGE, type HospitalQuery } from '@/api/hospitals'

/**
 * What the discovery screen is showing, kept in the query string
 * (`?q=&city=&page=`) so back, forward and reload all return to it.
 */
export type HospitalFilters = HospitalQuery

const textOf = (value: string | null) => (value ?? '').trim().slice(0, HOSPITAL_FILTER_MAX_LENGTH).trim()

function pageOf(value: string | null): number {
  if (value === null || !/^[1-9]\d{0,3}$/.test(value)) return 1
  const page = Number(value)
  return page <= HOSPITALS_MAX_PAGE ? page : 1
}

/**
 * Read the filters from the query string. The address bar is user input: text
 * is trimmed and cut to what the API accepts, and a page that is not a whole
 * number in range is page 1 — so a hand-edited URL shows a list, never a
 * validation error.
 */
export function readFilters(params: URLSearchParams): HospitalFilters {
  return { search: textOf(params.get('q')), city: textOf(params.get('city')), page: pageOf(params.get('page')) }
}

/**
 * The query string with `changes` applied. A filter at its default is left
 * out of the URL altogether; parameters that are not filters are kept.
 */
export function withFilters(current: URLSearchParams, changes: Partial<HospitalFilters>): URLSearchParams {
  const { search, city, page } = { ...readFilters(current), ...changes }
  const next = new URLSearchParams(current)
  const set = (name: string, value: string) => (value ? next.set(name, value) : next.delete(name))
  set('q', search)
  set('city', city)
  set('page', page > 1 ? String(page) : '')
  return next
}
