import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { http } from '@/api/http'

/** User status as stored by the backend (app/models/user.py UserStatus). */
export type UserStatus = 'active' | 'invited' | 'suspended' | 'deactivated'

export interface RoleSummary {
  id: string
  name: string
  description: string | null
  is_system: boolean
}

/** A user record as returned by GET /users and GET /users/{id}. */
export interface ManagedUser {
  id: string
  email: string
  first_name: string
  last_name: string
  phone: string | null
  status: UserStatus | string
  hospital_id: string | null
  roles: RoleSummary[]
  mfa_enabled: boolean
  last_login_at: string | null
  password_changed_at: string | null
  created_at: string | null
  updated_at: string | null
}

export interface UserListParams {
  page?: number
  pageSize?: number
  status?: UserStatus
  search?: string
}

export interface InviteUserInput {
  email: string
  first_name: string
  last_name: string
  phone?: string
  role_ids: string[]
}

/**
 * What happened to the invitation email. `queued` means an email was queued
 * for the address — delivery is not confirmed. `unavailable` means none was
 * sent because email delivery is not set up; the user stays Invited.
 */
export type InvitationDelivery = 'queued' | 'unavailable'

export interface InvitationResult {
  delivery: InvitationDelivery
}

/** The user created by POST /users, with what happened to their invitation. */
export type InvitedUser = ManagedUser & { invitation: InvitationResult }

export interface UpdateUserInput {
  first_name?: string
  last_name?: string
  /** `null` clears the number — an empty string is not a valid phone. */
  phone?: string | null
}

/** Query-key factory — `["users", ...]` (CLAUDE.md React Query patterns). */
export const userKeys = {
  all: ['users'] as const,
  list: (params: UserListParams) => [...userKeys.all, 'list', params] as const,
  detail: (id: string) => [...userKeys.all, 'detail', id] as const,
  roles: (id: string) => [...userKeys.all, 'roles', id] as const,
}

export const roleKeys = {
  all: ['roles'] as const,
  list: () => [...roleKeys.all, 'list'] as const,
}

// ── Queries ─────────────────────────────────────────────────────────────────

export function useUsers(params: UserListParams = {}) {
  return useQuery({
    queryKey: userKeys.list(params),
    queryFn: () =>
      http.getPaginated<ManagedUser>('/users', {
        params: {
          page: params.page,
          page_size: params.pageSize,
          status: params.status,
          search: params.search,
        },
      }),
    staleTime: 30_000,
  })
}

export function useUser(id: string | null) {
  return useQuery({
    queryKey: userKeys.detail(id ?? ''),
    queryFn: () => http.get<ManagedUser>(`/users/${id}`),
    enabled: !!id,
  })
}

/** Roles visible to the caller — the assignable catalog for invite/edit. */
export function useRoles() {
  return useQuery({
    queryKey: roleKeys.list(),
    queryFn: () => http.getPaginated<RoleSummary>('/roles', { params: { page_size: 100 } }),
    staleTime: 5 * 60_000,
  })
}

// ── Mutations ───────────────────────────────────────────────────────────────

function useInvalidateUsers() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: userKeys.all })
  }
}

/**
 * Reads an invitation result off the wire. Anything other than an explicit
 * `queued` — a missing field, a value this build does not know — is reported
 * as `unavailable`, so an unknown outcome is never shown as a sent email.
 */
function toInvitationResult(wire: unknown): InvitationResult {
  const delivery =
    typeof wire === 'object' && wire !== null ? (wire as { delivery?: unknown }).delivery : undefined
  return { delivery: delivery === 'queued' ? 'queued' : 'unavailable' }
}

export function useInviteUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: async (input: InviteUserInput): Promise<InvitedUser> => {
      const wire = await http.post<ManagedUser & { invitation?: unknown }>('/users', input)
      // Only the fields named here are kept. The activation credential is
      // delivered by email and must never be held by this app, so anything
      // else a response carries is dropped before it reaches the query cache.
      return {
        id: wire.id,
        email: wire.email,
        first_name: wire.first_name,
        last_name: wire.last_name,
        phone: wire.phone,
        status: wire.status,
        hospital_id: wire.hospital_id,
        roles: wire.roles,
        mfa_enabled: wire.mfa_enabled,
        last_login_at: wire.last_login_at,
        password_changed_at: wire.password_changed_at,
        created_at: wire.created_at,
        updated_at: wire.updated_at,
        invitation: toInvitationResult(wire.invitation),
      }
    },
    onSuccess: invalidate,
  })
}

/** Sends the invitation again to a user who is still Invited. */
export function useResendInvitation() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: async (id: string): Promise<InvitationResult> =>
      toInvitationResult(await http.post<unknown>(`/users/${id}/invitation`)),
    // Also after a refusal: a 409 means the row is no longer Invited.
    onSettled: invalidate,
  })
}

export function useUpdateUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: ({ id, ...input }: UpdateUserInput & { id: string }) =>
      http.patch<ManagedUser>(`/users/${id}`, input),
    onSuccess: invalidate,
  })
}

export function useDeactivateUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: (id: string) => http.post<ManagedUser>(`/users/${id}/deactivate`),
    onSuccess: invalidate,
  })
}

export function useReactivateUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: (id: string) => http.post<ManagedUser>(`/users/${id}/reactivate`),
    onSuccess: invalidate,
  })
}

export function useAssignRole() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: ({ userId, roleId }: { userId: string; roleId: string }) =>
      http.post<unknown>(`/users/${userId}/roles`, { role_id: roleId }),
    onSuccess: invalidate,
  })
}

export function useRemoveRole() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: ({ userId, roleId }: { userId: string; roleId: string }) =>
      http.delete<unknown>(`/users/${userId}/roles/${roleId}`),
    onSuccess: invalidate,
  })
}
