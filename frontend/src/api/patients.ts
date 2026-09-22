import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import type { Paginated } from '@/api/types'

/**
 * Patient API client. Shapes here mirror the wire format exactly — see
 * `docs/18-API_CONTRACTS.md` §2, which was verified against the backend
 * schemas and tests.
 *
 * Field names stay snake_case rather than being remapped to camelCase: the
 * envelope and pagination are already normalized in `http.ts`, and adding a
 * per-field mapping layer for every module is where contract drift creeps back
 * in. What the backend sends is what components read.
 */

/** The `gender_enum` values. Not a binary — the backend records `other` and `unspecified` as themselves. */
export type Gender = 'male' | 'female' | 'other' | 'unspecified'

/**
 * Record lifecycle, derived from the soft-delete column. This is **not** a
 * clinical state: there is no admissions module, so nothing reports whether a
 * patient is admitted, discharged or critical.
 */
export type PatientStatus = 'active' | 'inactive'

export const GENDER_LABELS: Record<Gender, string> = {
  male: 'Male',
  female: 'Female',
  other: 'Other',
  unspecified: 'Not specified',
}

/** Row shape from `GET /patients` — identity only, no medical history. */
export interface PatientSummary {
  id: string
  /** Medical Record Number, e.g. `MRN-2026-00042`. Server-generated, immutable. */
  mrn: string
  first_name: string
  last_name: string
  full_name: string
  /** ISO date, `YYYY-MM-DD`. */
  date_of_birth: string
  /** Completed years, computed by the backend at response time. */
  age: number
  gender: Gender
  phone: string | null
  status: PatientStatus
}

/** Full record from create / get / update. */
export interface Patient extends PatientSummary {
  hospital_id: string
  blood_group: string | null
  email: string | null
  address: Record<string, unknown> | null
  emergency_contact: Record<string, unknown> | null
  marital_status: string | null
  occupation: string | null
  allergies: Record<string, unknown>[]
  chronic_conditions: Record<string, unknown>[]
  current_medications: Record<string, unknown>[]
  notes: string | null
  created_at: string
  updated_at: string
}

/**
 * Query parameters for the list endpoint. The names are the backend's: `q`
 * (not `search`) and `page_size` (not `pageSize`).
 *
 * `q` prefix-matches first or last name case-insensitively, and exact-matches
 * MRN or phone. It is not a substring search — "ao" will not find "Rao".
 */
export interface PatientListParams {
  q?: string
  gender?: Gender
  age_gte?: number
  age_lte?: number
  include_inactive?: boolean
  page?: number
  page_size?: number
}

/** Body for `POST /patients`. No `age` and no `mrn` — the backend derives both. */
export interface CreatePatientInput {
  first_name: string
  last_name: string
  date_of_birth: string
  gender: Gender
  phone?: string
  email?: string
  blood_group?: string
}

/** Query-key factory — `["patients", ...]` (CLAUDE.md React Query patterns). */
export const patientKeys = {
  all: ['patients'] as const,
  list: (params: PatientListParams) => [...patientKeys.all, 'list', params] as const,
  detail: (id: string) => [...patientKeys.all, 'detail', id] as const,
}

function fetchPatients(params: PatientListParams): Promise<Paginated<PatientSummary>> {
  return http.getPaginated<PatientSummary>('/patients', { params })
}

function createPatient(input: CreatePatientInput): Promise<Patient> {
  return http.post<Patient>('/patients', input)
}

// ---------------------------------------------------------------------------
// Hooks — the only way components touch patient data (CLAUDE.md).
// ---------------------------------------------------------------------------

export function usePatients(params: PatientListParams = {}) {
  return useQuery({
    queryKey: patientKeys.list(params),
    queryFn: () => fetchPatients(params),
    staleTime: 30_000,
    // Keeps the current page on screen while the next one loads, so paging and
    // typing in the search box don't flash an empty table.
    placeholderData: (previous) => previous,
  })
}

export function usePatient(id: string) {
  return useQuery({
    queryKey: patientKeys.detail(id),
    queryFn: () => http.get<Patient>(`/patients/${id}`),
    enabled: Boolean(id),
  })
}

export function useCreatePatient() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: createPatient,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: patientKeys.all })
    },
  })
}
