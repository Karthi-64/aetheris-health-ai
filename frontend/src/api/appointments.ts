import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import type { Paginated } from '@/api/types'

/**
 * Appointment module API — typed hooks over the real backend contract
 * (`backend/app/api/v1/appointments.py`, `backend/app/schemas/appointment.py`).
 */

export type AppointmentStatus =
  | 'booked'
  | 'checked_in'
  | 'in_progress'
  | 'completed'
  | 'cancelled'
  | 'no_show'

export type AppointmentType = 'new' | 'follow_up' | 'walk_in' | 'emergency'

/** Compact shape from the list endpoint (`AppointmentSummaryResponse`). */
export interface AppointmentSummary {
  id: string
  patient_id: string
  patient_name: string
  doctor_id: string
  doctor_name: string
  scheduled_start: string
  scheduled_end: string
  status: AppointmentStatus
  type: AppointmentType
}

/** Full record from get/book (`AppointmentResponse`). */
export interface Appointment extends AppointmentSummary {
  hospital_id: string
  reason: string | null
  notes: string | null
  cancelled_reason: string | null
  checked_in_at: string | null
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

/** Body for `POST /appointments` (`BookAppointmentRequest`). Timestamps are tz-aware ISO. */
export interface BookAppointmentInput {
  patient_id: string
  doctor_id: string
  scheduled_start: string
  scheduled_end: string
  type: AppointmentType
  reason?: string
  notes?: string
}

export interface AppointmentListParams {
  patient_id?: string
  doctor_id?: string
  /** Local calendar day (YYYY-MM-DD); interpreted with `tz_offset_hours`. */
  appointment_date?: string
  appointment_status?: AppointmentStatus
  appointment_type?: AppointmentType
  page?: number
  page_size?: number
}

/** Whole-hour offset from UTC for the viewer, e.g. UTC+5 -> 5 (the API takes an int). */
export function localTzOffsetHours(): number {
  return Math.round(-new Date().getTimezoneOffset() / 60)
}

export const appointmentKeys = {
  all: ['appointments'] as const,
  list: (params: AppointmentListParams) => [...appointmentKeys.all, 'list', params] as const,
  detail: (id: string) => [...appointmentKeys.all, 'detail', id] as const,
}

/** List appointments. For the day queue, pass `appointment_date`. */
export function useAppointments(params: AppointmentListParams = {}) {
  const withTz = { ...params, tz_offset_hours: localTzOffsetHours() }
  return useQuery<Paginated<AppointmentSummary>>({
    queryKey: appointmentKeys.list(params),
    queryFn: () => http.getPaginated<AppointmentSummary>('/appointments', { params: withTz }),
    staleTime: 15_000,
    placeholderData: keepPreviousData,
  })
}

/** Book an appointment, then refresh the queue. Throws ApiError (409 on doctor overlap). */
export function useBookAppointment() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: BookAppointmentInput) => http.post<Appointment>('/appointments', input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: appointmentKeys.all })
    },
  })
}
