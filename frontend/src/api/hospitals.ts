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

/** One gated feature, as `GET /hospitals/current/feature-flags` reports it. */
export interface FeatureFlagState {
  /** The hospital's stored flag is exactly `true` AND the server is configured to serve the feature. Configured is not the same as reachable. */
  available: boolean
}

/** Answer of `GET /hospitals/current/feature-flags`. */
export interface FeatureFlags {
  /** Only the well-known flag keys; never other hospital settings. */
  flags: Record<string, FeatureFlagState>
}

/** AI slot suggestions in the booking dialog. */
export const AI_SLOT_RECOMMENDATION_FLAG = 'feature.ai.slot_recommendation'

export const hospitalKeys = {
  all: ['hospitals'] as const,
  current: () => [...hospitalKeys.all, 'current'] as const,
  full: () => [...hospitalKeys.all, 'full'] as const,
  featureFlags: () => [...hospitalKeys.all, 'feature-flags'] as const,
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

/**
 * Which gated features this hospital can use right now. Any signed-in user may
 * read it. Never retried: a caller treats anything but an explicit
 * `available: true` as "not available".
 */
export function useFeatureFlags(enabled = true) {
  return useQuery({
    queryKey: hospitalKeys.featureFlags(),
    queryFn: () => http.get<FeatureFlags>('/hospitals/current/feature-flags'),
    enabled,
    staleTime: 5 * 60_000,
    retry: false,
  })
}

/**
 * True only when the capability read says the flag is available. Loading, a
 * failed read or a body of another shape all mean "no".
 */
export function isFeatureAvailable(flags: unknown, key: string): boolean {
  if (typeof flags !== 'object' || flags === null) return false
  const map = (flags as { flags?: unknown }).flags
  if (typeof map !== 'object' || map === null) return false
  const state = (map as Record<string, unknown>)[key]
  return typeof state === 'object' && state !== null && (state as { available?: unknown }).available === true
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
