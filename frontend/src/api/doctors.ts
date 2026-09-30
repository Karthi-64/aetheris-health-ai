import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { http } from '@/api/http'
import type { Paginated } from '@/api/types'

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
  consultation_fee: string | number
  status: DoctorStatus
}

/** Full record from get (`DoctorResponse`). */
export interface Doctor extends DoctorSummary {
  hospital_id: string
  email: string | null
  license_number: string
  qualifications: Array<Record<string, unknown>>
  languages: string[]
  bio: string | null
  created_at: string
  updated_at: string
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
  list: (params: DoctorListParams) => [...doctorKeys.all, 'list', params] as const,
  detail: (id: string) => [...doctorKeys.all, 'detail', id] as const,
}

/** List / search doctors. Supports `q`, `specialization`, `department`, page/size. */
export function useDoctors(params: DoctorListParams = {}) {
  return useQuery<Paginated<DoctorSummary>>({
    queryKey: doctorKeys.list(params),
    queryFn: () => http.getPaginated<DoctorSummary>('/doctors', { params }),
    staleTime: 30_000,
    placeholderData: keepPreviousData,
  })
}

/** Fetch one doctor's full record. */
export function useDoctor(id: string | undefined) {
  return useQuery<Doctor>({
    queryKey: doctorKeys.detail(id ?? ''),
    queryFn: () => http.get<Doctor>(`/doctors/${id}`),
    enabled: !!id,
  })
}
