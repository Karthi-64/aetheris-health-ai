import { DOCTOR_SEARCH_MAX_LENGTH, DOCTORS_MAX_PAGE, type DoctorQuery } from '@/api/doctors'

/**
 * What a hospital's doctor list is showing, kept in the query string
 * (`?q=&dept=&page=`) so back, forward and reload all return to it. The
 * hospital itself is in the path, not here.
 */
export type DoctorFilters = Omit<DoctorQuery, 'hospitalRef'>

const textOf = (value: string | null) => (value ?? '').trim().slice(0, DOCTOR_SEARCH_MAX_LENGTH).trim()

function pageOf(value: string | null): number {
  if (value === null || !/^[1-9]\d{0,3}$/.test(value)) return 1
  const page = Number(value)
  return page <= DOCTORS_MAX_PAGE ? page : 1
}

/**
 * Read the filters from the query string. The address bar is user input: text
 * is trimmed and cut to what the API accepts, and a page that is not a whole
 * number in range is page 1. A department is kept as it was written — one that
 * is not a reference is still a filter in force, and it matches no doctor
 * (`useDoctors` does not send it).
 */
export function readDoctorFilters(params: URLSearchParams): DoctorFilters {
  return { search: textOf(params.get('q')), department: textOf(params.get('dept')), page: pageOf(params.get('page')) }
}

/**
 * The query string with `changes` applied. A filter at its default is left
 * out of the URL altogether; parameters that are not filters are kept.
 */
export function withDoctorFilters(current: URLSearchParams, changes: Partial<DoctorFilters>): URLSearchParams {
  const { search, department, page } = { ...readDoctorFilters(current), ...changes }
  const next = new URLSearchParams(current)
  const set = (name: string, value: string) => (value ? next.set(name, value) : next.delete(name))
  set('q', search)
  set('dept', department)
  set('page', page > 1 ? String(page) : '')
  return next
}
