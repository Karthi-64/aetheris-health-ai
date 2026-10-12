import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import { ApiError, type Paginated, type ListQueryOptions } from '@/api/types'

/**
 * Doctor module API — typed hooks over the real backend contract
 * (`backend/app/api/v1/doctors.py`, `backend/app/schemas/doctor.py`).
 */

export type DoctorStatus = 'active' | 'inactive'

/** Compact shape from the list endpoint (`DoctorSummaryResponse`). */
export interface DoctorSummary {
  id: string
  user_id: string
  full_name: string
  specialization: string
  department_id: string | null
  department_name: string | null
  /** A decimal as text ("950.00"). The API never sends a JSON number. */
  consultation_fee: string
  status: DoctorStatus
}

/**
 * One qualification. Only `degree` is required: an item saved without the
 * optional keys comes back without them, not with nulls.
 */
export interface Qualification {
  degree: string
  institution?: string | null
  year?: number | null
}

/** Full record from get (`DoctorResponse`). */
export interface Doctor extends DoctorSummary {
  hospital_id: string
  /** From the linked user account, like `full_name`. Neither can be changed through a doctor. */
  email: string | null
  license_number: string
  qualifications: Qualification[]
  languages: string[]
  bio: string | null
  created_at: string
  updated_at: string
}

/**
 * Body of `PATCH /doctors/{id}`: a partial update of exactly these keys. Any
 * other key is a 422 and so is an empty body, so send only what changed.
 * `null` is accepted only where the type allows it.
 */
export interface UpdateDoctorInput {
  specialization?: string
  license_number?: string
  /** A decimal as text, 0 to 999999.99 with at most two decimals. */
  consultation_fee?: string
  /** `null` unassigns. Otherwise an active department of the hospital. */
  department_id?: string | null
  /** Replaces the whole list; `[]` clears it. */
  qualifications?: Qualification[]
  /** Replaces the whole list; `[]` clears it. */
  languages?: string[]
  /** `null` clears. An empty string would be stored as one. */
  bio?: string | null
}

export interface DoctorListParams {
  q?: string
  specialization?: string
  /** Department UUID to filter by. */
  department?: string
  include_inactive?: boolean
  page?: number
  page_size?: number
}

export const doctorKeys = {
  all: ['doctors'] as const,
  lists: () => [...doctorKeys.all, 'list'] as const,
  list: (params: DoctorListParams) => [...doctorKeys.lists(), params] as const,
  detail: (id: string) => [...doctorKeys.all, 'detail', id] as const,
  /** Every doctor's computed slots. Booking, cancelling or moving an appointment changes them. */
  slots: () => [...doctorKeys.all, 'slots'] as const,
  slotsFor: (id: string, date: string) => [...doctorKeys.slots(), id, date] as const,
}

/** List / search doctors. Supports `q`, `specialization`, `department`, page/size. */
export function useDoctors(params: DoctorListParams = {}, options: ListQueryOptions = {}) {
  return useQuery<Paginated<DoctorSummary>>({
    enabled: options.enabled ?? true,
    queryKey: doctorKeys.list(params),
    queryFn: () => http.getPaginated<DoctorSummary>('/doctors', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

export type SlotStatus = 'available' | 'booked' | 'on_leave'

/** One computed slot (`SlotResponse`). `start` and `end` carry the hospital's UTC offset. */
export interface DoctorSlot {
  start: string
  end: string
  status: SlotStatus
  appointment_id: string | null
}

/** A doctor's slots for one day (`DaySlotsResponse`). */
export interface DaySlots {
  /** `YYYY-MM-DD`. */
  date: string
  doctor_id: string
  /** IANA zone the slot times are in — the hospital's, not the viewer's. */
  timezone: string
  slots: DoctorSlot[]
}

/**
 * A doctor's slots for one calendar day (docs/18-API_CONTRACTS.md §4.6).
 *
 * Slots are computed on demand from availability, leave and bookings — there
 * is no slot id to book against; an appointment is booked by sending a slot's
 * `start` and `end` back. Needs `doctor.availability.read`.
 */
export function useDoctorSlots(id: string, date: string, options: ListQueryOptions = {}) {
  return useQuery<DaySlots>({
    enabled: (options.enabled ?? true) && !!id && !!date,
    queryKey: doctorKeys.slotsFor(id, date),
    queryFn: () => http.get<DaySlots>(`/doctors/${id}/slots`, { params: { date } }),
    // Another desk can take a slot at any moment; never show a cached day.
    staleTime: 0,
  })
}

/**
 * Fetch one doctor's full record. A deactivated doctor is a 404 unless
 * `includeInactive` is set, which the screen that can reactivate one needs.
 *
 * The flag is not part of the query key: it decides whether a deactivated
 * record is returned, not what the record is, and the mutations below write
 * the server's record to this one entry.
 */
export function useDoctor(id: string | undefined, options: { includeInactive?: boolean } = {}) {
  return useQuery<Doctor>({
    queryKey: doctorKeys.detail(id ?? ''),
    queryFn: () =>
      http.get<Doctor>(
        `/doctors/${id}`,
        options.includeInactive ? { params: { include_inactive: true } } : undefined,
      ),
    enabled: !!id,
  })
}

/**
 * Shared plumbing for the writes on one doctor. Each returns the full record,
 * which goes straight into the detail cache so the screen shows the server's
 * values; the lists are refetched because a row repeats several of them.
 *
 * A read of the doctor still in flight is dropped first. It may have been
 * answered before the write, and would put the old record back over the new
 * one when it landed.
 *
 * A 400, 404 or 409 means the doctor was not in the state the screen showed
 * (already deactivated, has appointments booked since), so every doctor query
 * is refetched on those.
 */
function useDoctorMutation<TInput>(id: string, mutationFn: (input: TInput) => Promise<Doctor>) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn,
    onSuccess: async (doctor) => {
      await qc.cancelQueries({ queryKey: doctorKeys.detail(id) })
      qc.setQueryData(doctorKeys.detail(id), doctor)
      qc.invalidateQueries({ queryKey: doctorKeys.lists() })
    },
    onError: (err) => {
      if (err instanceof ApiError && [400, 404, 409].includes(err.status ?? 0)) {
        qc.invalidateQueries({ queryKey: doctorKeys.all })
      }
    },
  })
}

/** Edit a doctor's profile. A deactivated doctor is a 404 here until reactivated. */
export function useUpdateDoctor(id: string) {
  return useDoctorMutation(id, (input: UpdateDoctorInput) =>
    http.patch<Doctor>(`/doctors/${id}`, input),
  )
}

/** Deactivate a doctor. No body; 409 while the doctor has future appointments. */
export function useDeactivateDoctor(id: string) {
  return useDoctorMutation<void>(id, () => http.delete<Doctor>(`/doctors/${id}`))
}

/** Reactivate a deactivated doctor. No body; needs `doctor.update`, not `doctor.delete`. */
export function useActivateDoctor(id: string) {
  return useDoctorMutation<void>(id, () => http.post<Doctor>(`/doctors/${id}/activate`))
}
