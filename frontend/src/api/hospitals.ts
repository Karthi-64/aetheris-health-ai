import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'

/** Hospital record contract (`backend/app/api/v1/hospitals.py`). */
export interface HospitalPublic {
  id: string
  name: string
  slug: string
  address: Record<string, string>
  phone: string | null
  email: string | null
  logo_url: string | null
  timezone: string
  currency: string
  locale: string
  is_active: boolean
}

/** Full settings record (GET /hospitals/current/full). */
export interface HospitalSettings extends HospitalPublic {
  tax_id: string | null
  settings: Record<string, unknown>
  created_at: string
  updated_at: string
}

/** Editable fields only — slug/timezone/currency/tax_id are Superadmin-reserved. */
export interface UpdateHospitalSettingsInput {
  name?: string
  address?: Record<string, string>
  phone?: string | null
  email?: string | null
  locale?: string
  logo_url?: string | null
  settings?: Record<string, unknown>
}

export const hospitalKeys = {
  all: ['hospitals'] as const,
  current: () => [...hospitalKeys.all, 'current'] as const,
  full: () => [...hospitalKeys.all, 'full'] as const,
}

/** Public hospital record — used by any authenticated shell. */
export function useHospital() {
  return useQuery({
    queryKey: hospitalKeys.current(),
    queryFn: () => http.get<HospitalPublic>('/hospitals/current'),
    staleTime: 5 * 60_000,
  })
}

/** Full settings record for the Settings screen (requires settings.read). */
export function useHospitalSettings(enabled = true) {
  return useQuery({
    queryKey: hospitalKeys.full(),
    queryFn: () => http.get<HospitalSettings>('/hospitals/current/full'),
    enabled,
    staleTime: 60_000,
  })
}

/** PATCH /hospitals/current — requires settings.update. */
export function useUpdateHospitalSettings() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateHospitalSettingsInput) =>
      http.patch<HospitalSettings>('/hospitals/current', input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: hospitalKeys.all })
    },
  })
}
