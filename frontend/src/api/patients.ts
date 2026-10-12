import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { appointmentKeys } from '@/api/appointments'
import { billingKeys } from '@/api/billing'
import { http } from '@/api/http'
import { dashboardKeys, reportKeys } from '@/api/reports'
import { ApiError, type Paginated, type ListQueryOptions } from '@/api/types'

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

/** The `BloodGroup` values. A group that is not on record is `null` — the API rejects `''`. */
export type BloodGroup = 'A+' | 'A-' | 'B+' | 'B-' | 'AB+' | 'AB-' | 'O+' | 'O-'

export type AllergySeverity = 'mild' | 'moderate' | 'severe'

/**
 * The nested objects of a patient write (`backend/app/schemas/patient.py`).
 * The API rejects unknown keys inside each one and stores exactly the keys it
 * is sent, which is why the record reads them back as loose objects: an
 * optional key that was left out is absent, not `null`.
 */
export interface PatientAddress {
  line1: string
  line2?: string | null
  city: string
  state?: string | null
  postal_code?: string | null
  /** Two letters. The server uppercases it. */
  country: string
}

/** All three are required whenever a contact is sent. */
export interface EmergencyContact {
  name: string
  /** Normalized like the patient's phone, but may not be blank. */
  phone: string
  /** Free text, e.g. `mother`. */
  relation: string
}

export interface PatientAllergy {
  name: string
  /**
   * Never `null`. The API lets it be left out, but an update does not store
   * its `moderate` default for an omitted key — so it is always sent.
   */
  severity: AllergySeverity
  reaction?: string | null
  /** ISO date, `YYYY-MM-DD`. */
  noted_on?: string | null
}

export interface ChronicCondition {
  name: string
  /** 1900 up to the current year. */
  since_year?: number | null
  notes?: string | null
}

export interface PatientMedication {
  name: string
  dosage?: string | null
  frequency?: string | null
  /** ISO date, `YYYY-MM-DD`. */
  started_on?: string | null
}

/**
 * Body for `PATCH /patients/{id}`. Partial at the top level only: send the
 * keys that changed, and at least one — an empty body is a 422, as is any key
 * not listed here (`mrn`, `status`, `age` and the rest of the record).
 *
 * `null` is what clears a field, and only the fields typed with it accept it.
 * An address, an emergency contact and each history list replace what is
 * stored rather than merging into it, so they are always sent whole.
 */
export interface UpdatePatientInput {
  first_name?: string
  last_name?: string
  /** ISO date, `YYYY-MM-DD`. */
  date_of_birth?: string
  gender?: Gender
  blood_group?: BloodGroup | null
  phone?: string | null
  email?: string | null
  address?: PatientAddress | null
  emergency_contact?: EmergencyContact | null
  /** `''` is stored as an empty string, so an emptied field is sent as `null`. Same for `occupation` and `notes`. */
  marital_status?: string | null
  occupation?: string | null
  /** `[]` clears a list; `null` is rejected. */
  allergies?: PatientAllergy[]
  chronic_conditions?: ChronicCondition[]
  current_medications?: PatientMedication[]
  notes?: string | null
}

/** Query-key factory — `["patients", ...]` (CLAUDE.md React Query patterns). */
export const patientKeys = {
  all: ['patients'] as const,
  lists: () => [...patientKeys.all, 'list'] as const,
  list: (params: PatientListParams) => [...patientKeys.lists(), params] as const,
  detail: (id: string) => [...patientKeys.all, 'detail', id] as const,
}

function fetchPatients(params: PatientListParams): Promise<Paginated<PatientSummary>> {
  return http.getPaginated<PatientSummary>('/patients', { params })
}

function fetchPatient(id: string): Promise<Patient> {
  return http.get<Patient>(`/patients/${id}`)
}

function createPatient(input: CreatePatientInput): Promise<Patient> {
  return http.post<Patient>('/patients', input)
}

// ---------------------------------------------------------------------------
// Hooks — the only way components touch patient data (CLAUDE.md).
// ---------------------------------------------------------------------------

export function usePatients(params: PatientListParams = {}, options: ListQueryOptions = {}) {
  return useQuery({
    enabled: options.enabled ?? true,
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
    queryFn: () => fetchPatient(id),
    enabled: Boolean(id),
  })
}

/**
 * Returns a function that reads the patient from the server now, however
 * recent the cached copy is, and caches the answer.
 *
 * For a write that must not be built on a stale copy. Nothing refreshes an
 * open page, and the API has no version check on an update, so a caller about
 * to replace a whole list uses this to see what is stored at that moment.
 */
export function useFetchCurrentPatient(id: string) {
  const qc = useQueryClient()
  return async () => {
    // A read already in flight may have been answered before a colleague's
    // edit, and fetchQuery would join it rather than ask again.
    await qc.cancelQueries({ queryKey: patientKeys.detail(id) })
    return qc.fetchQuery({
      queryKey: patientKeys.detail(id),
      queryFn: () => fetchPatient(id),
      staleTime: 0,
      // Like the write it comes before, it fails at once rather than after a retry.
      retry: false,
    })
  }
}

export function useCreatePatient() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: createPatient,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: patientKeys.all })
      // A new patient moves the registration tile and the patients report.
      qc.invalidateQueries({ queryKey: dashboardKeys.all })
      qc.invalidateQueries({ queryKey: reportKeys.all })
    },
  })
}

/**
 * Update a patient. The response is the saved record with the server's
 * normalization applied (phone, email, country), so it goes straight into the
 * detail cache and the screen shows what was stored without a second request.
 *
 * A patient's name is copied into appointment and invoice rows, so those lists
 * are refetched along with the registry. A 404 means the record is gone or was
 * deactivated after it was loaded; the patient queries are refetched so the
 * screen stops offering an edit the API will keep refusing.
 */
export function useUpdatePatient(id: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdatePatientInput) => http.patch<Patient>(`/patients/${id}`, input),
    onSuccess: async (updated) => {
      // A read still in flight may have been answered before the write, and
      // would put the old record back over the new one when it landed.
      await qc.cancelQueries({ queryKey: patientKeys.detail(id) })
      qc.setQueryData(patientKeys.detail(id), updated)
      qc.invalidateQueries({ queryKey: patientKeys.lists() })
      qc.invalidateQueries({ queryKey: appointmentKeys.all })
      qc.invalidateQueries({ queryKey: billingKeys.all })
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 404) {
        qc.invalidateQueries({ queryKey: patientKeys.all })
      }
    },
  })
}
