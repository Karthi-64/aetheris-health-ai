import { useQuery } from '@tanstack/react-query'
import { ApiError } from '@atheris/api-core'
import { http } from '@/api/client'

/**
 * Hospital discovery. This is reference data, not patient data: the same for
 * every signed-in patient except `linked`, which the server works out from the
 * caller's own account. Nothing here sends an account, a patient or a hospital
 * id — a hospital is named only by its public reference.
 */

/** The parts of an address the API returns. Any of them may be missing. */
export interface HospitalAddress {
  line1: string | null
  line2: string | null
  city: string | null
  state: string | null
  postal_code: string | null
  country: string | null
}

/**
 * How a hospital is listed. Every hospital is `standard` today; `promoted` is
 * reserved for paid placement and is always labelled as such on screen.
 */
export type HospitalListing = 'standard' | 'promoted'

/** One hospital, as the Patient App may show it. */
export interface PatientHospital {
  /** The public reference — the code a hospital gives its patients. */
  ref: string
  name: string
  address: HospitalAddress
  phone: string | null
  logo_url: string | null
  /** IANA zone name, e.g. `Asia/Kolkata`. */
  timezone: string
  /** True when this account holds a link at the hospital that is honoured right now. */
  linked: boolean
  listing: HospitalListing
}

/** What `GET /hospitals` is asked for. Empty text means "no filter". */
export interface HospitalQuery {
  search: string
  city: string
  page: number
}

/** The limits of `GET /hospitals`: anything beyond them is a 422. */
export const HOSPITALS_PAGE_SIZE = 20
export const HOSPITAL_FILTER_MAX_LENGTH = 80
export const HOSPITALS_MAX_PAGE = 1000

export const hospitalKeys = {
  /** Every list and every hospital: what `linked` going stale invalidates. */
  all: ['patient', 'hospitals'] as const,
  list: (query: HospitalQuery) => ['patient', 'hospitals', 'list', query] as const,
  detail: (hospitalRef: string) => ['patient', 'hospitals', 'detail', hospitalRef] as const,
  cities: ['patient', 'hospital-cities'] as const,
}

/** One page of the hospitals available in the app, by name and city. */
export function useHospitals({ search, city, page }: HospitalQuery) {
  return useQuery({
    queryKey: hospitalKeys.list({ search, city, page }),
    queryFn: async () => {
      const result = await http.getPaginated<PatientHospital>('/hospitals', {
        params: {
          page,
          page_size: HOSPITALS_PAGE_SIZE,
          ...(search ? { search } : {}),
          ...(city ? { city } : {}),
        },
      })
      // A page that is not a list can be neither shown nor counted: it is a failed read.
      if (!Array.isArray(result.items)) throw new ApiError('Response is not a list', 'bad_response')
      return result
    },
  })
}

/**
 * A reference is a hospital's code or its id: letters, digits and hyphens.
 * Anything else names no hospital, so it is never sent. That matters for more
 * than tidiness: `.` and `..` come through `encodeURIComponent` unchanged and
 * a browser resolves them as path steps, and a server decodes an escaped `/`
 * back into one — either would leave as a request to a different endpoint.
 */
export const isSendable = (hospitalRef: string) => /^[A-Za-z0-9-]{1,100}$/.test(hospitalRef)

async function fetchHospital(hospitalRef: string): Promise<PatientHospital> {
  // The same answer the server gives for a reference it does not know.
  if (!isSendable(hospitalRef)) throw new ApiError('Not found.', 'RESOURCE_NOT_FOUND', 404)
  return http.get<PatientHospital>(`/hospitals/${encodeURIComponent(hospitalRef)}`)
}

/** One hospital by its public reference. Unknown and unavailable are the same 404. */
export function useHospital(hospitalRef: string) {
  return useQuery({
    queryKey: hospitalKeys.detail(hospitalRef),
    queryFn: () => fetchHospital(hospitalRef),
  })
}

/** The cities the listed hospitals are in: the options of the city filter. */
export function useHospitalCities() {
  return useQuery({
    queryKey: hospitalKeys.cities,
    queryFn: async () => {
      const { cities } = await http.get<{ cities: string[] }>('/hospital-cities')
      // Options are built from these; anything that is not a name is not one.
      return Array.isArray(cities) ? cities.filter((city) => typeof city === 'string' && city.trim() !== '') : []
    },
  })
}
