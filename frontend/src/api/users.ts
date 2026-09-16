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

export interface UpdateUserInput {
  first_name?: string
  last_name?: string
  phone?: string
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

export function useInviteUser() {
  const invalidate = useInvalidateUsers()
  return useMutation({
    mutationFn: (input: InviteUserInput) =>
      http.post<ManagedUser & { invite_token?: string }>('/users', input),
    onSuccess: invalidate,
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
