import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'
import type { ManagedUser } from '@/api/users'

/**
 * Self-service profile (module spec `02-user-management.md` §12, "Own profile
 * page"). Every endpoint here acts on the caller's *own* account and is
 * therefore gated by authentication alone — no `user.*` permission is needed,
 * which is why these live apart from the admin directory in `api/users.ts`.
 */

export interface UpdateProfileInput {
  first_name?: string
  last_name?: string
  /** E.164, or omitted entirely — the backend rejects an empty string. */
  phone?: string
}

export interface ChangePasswordInput {
  current_password: string
  new_password: string
}

export interface MfaEnrollment {
  secret: string
  provisioning_uri: string
}

export const profileKeys = {
  me: ['profile', 'me'] as const,
}

/** The caller's own profile — `GET /users/me`. */
export function useMyProfile() {
  return useQuery({
    queryKey: profileKeys.me,
    queryFn: () => http.get<ManagedUser>('/users/me'),
    staleTime: 60_000,
  })
}

export function useUpdateMyProfile() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: UpdateProfileInput) => http.patch<ManagedUser>('/users/me', input),
    onSuccess: (updated) => {
      qc.setQueryData(profileKeys.me, updated)
    },
  })
}

export function useChangePassword() {
  return useMutation({
    mutationFn: (input: ChangePasswordInput) => http.post<unknown>('/auth/password/change', input),
  })
}

export function useEnrollMfa() {
  return useMutation({
    mutationFn: (password: string) => http.post<MfaEnrollment>('/auth/mfa/enroll', { password }),
  })
}

export function useConfirmMfa() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { secret: string; code: string }) =>
      http.post<unknown>('/auth/mfa/confirm', input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: profileKeys.me })
    },
  })
}

export function useDisableMfa() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: { password: string; code: string }) =>
      http.post<unknown>('/auth/mfa/disable', input),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: profileKeys.me })
    },
  })
}
