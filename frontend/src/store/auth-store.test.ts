import { describe, it, expect, beforeEach, vi } from 'vitest'
import { queryClient } from '@/lib/query-client'
import { toAuthUser, useAuthStore, type AuthUser } from './auth-store'

describe('toAuthUser', () => {
  it('maps the backend login user payload to the store shape', () => {
    const user = toAuthUser({
      id: 'u1',
      email: 'admin@hospital.test',
      first_name: 'Admin',
      last_name: 'User',
      roles: ['Hospital Admin'],
      permissions: ['user.read', 'patient.read', 'dashboard.view'],
      status: 'active',
    })

    expect(user.id).toBe('u1')
    expect(user.name).toBe('Admin User')
    expect(user.email).toBe('admin@hospital.test')
    expect(user.role).toBe('hospital_admin')
    expect(user.permissions).toEqual(['user.read', 'patient.read', 'dashboard.view'])
  })

  it('keeps an explicit name field when the payload provides one', () => {
    const user = toAuthUser({ id: 'u2', name: 'Dr. Smith', email: 's@h.test', first_name: '', last_name: '' })
    expect(user.name).toBe('Dr. Smith')
  })

  it('falls back to User when no name can be derived', () => {
    const user = toAuthUser({ id: 'u3', email: 'x@y.test' })
    expect(user.name).toBe('User')
  })

  it('maps unknown role display names to undefined role (display-only)', () => {
    const user = toAuthUser({ id: 'u4', email: 'a@b.test', first_name: 'A', last_name: 'B', roles: ['Physiotherapist'] })
    expect(user.role).toBeUndefined()
  })

  it('never fabricates permissions when the payload omits them', () => {
    const user = toAuthUser({ id: 'u5', email: 'a@b.test', first_name: 'A', last_name: 'B' })
    expect(user.permissions).toEqual([])
  })
})

describe('auth store', () => {
  beforeEach(() => {
    useAuthStore.getState().logout()
  })

  it('stores the server-issued permissions verbatim', () => {
    useAuthStore.getState().setAuth(
      toAuthUser({
        id: 'u1',
        email: 'a@b.test',
        first_name: 'A',
        last_name: 'B',
        permissions: ['user.read'],
      }),
      'access-token',
      'refresh-token',
    )

    const { user, isAuthenticated } = useAuthStore.getState()
    expect(isAuthenticated).toBe(true)
    expect(user?.permissions).toEqual(['user.read'])
  })
})

/**
 * The query cache is keyed by resource, not by user. Whatever one user loaded
 * must be gone before the next one signs in in the same tab.
 */
describe('auth store and the query cache', () => {
  const ADMIN: AuthUser = { id: 'admin-1', name: 'Admin', email: 'a@h.test', permissions: [] }
  const DOCTOR: AuthUser = { id: 'doc-1', name: 'Doctor', email: 'd@h.test', permissions: [] }
  const KEY = ['invoices', 'list']

  /** What a page does: read through the cache, fetching only when it has nothing fresh. */
  const loadInvoices = (server: () => Promise<string[]>) =>
    queryClient.fetchQuery({ queryKey: KEY, queryFn: server })

  beforeEach(() => {
    useAuthStore.getState().logout()
    queryClient.clear()
  })

  it('does not show the next user what the previous one loaded', async () => {
    const { setAuth, logout } = useAuthStore.getState()

    setAuth(ADMIN, 'admin-token', 'admin-refresh')
    await loadInvoices(async () => ['every invoice in the hospital'])
    expect(queryClient.getQueryData(KEY)).toEqual(['every invoice in the hospital'])

    logout()
    expect(queryClient.getQueryData(KEY)).toBeUndefined()

    setAuth(DOCTOR, 'doctor-token', 'doctor-refresh')
    expect(queryClient.getQueryData(KEY)).toBeUndefined()
    const server = vi.fn(async () => ['the doctor’s own invoices'])
    expect(await loadInvoices(server)).toEqual(['the doctor’s own invoices'])
    expect(server).toHaveBeenCalledTimes(1)
  })

  it('clears the cache when a different user signs in without a sign-out in between', async () => {
    const { setAuth } = useAuthStore.getState()

    setAuth(ADMIN, 'admin-token')
    await loadInvoices(async () => ['every invoice in the hospital'])

    setAuth(DOCTOR, 'doctor-token')
    expect(queryClient.getQueryData(KEY)).toBeUndefined()
  })

  it('keeps the cache when the same user is given fresh tokens', async () => {
    const { setAuth } = useAuthStore.getState()

    setAuth(ADMIN, 'admin-token')
    await loadInvoices(async () => ['every invoice in the hospital'])

    setAuth(ADMIN, 'admin-token-2')
    expect(queryClient.getQueryData(KEY)).toEqual(['every invoice in the hospital'])
  })

  it('cancels a request still in flight at sign-out, so its answer is never cached', async () => {
    const { setAuth, logout } = useAuthStore.getState()
    setAuth(ADMIN, 'admin-token')

    let answer: (rows: string[]) => void = () => {}
    const pending = loadInvoices(() => new Promise<string[]>((resolve) => (answer = resolve)))
    pending.catch(() => {})
    await vi.waitFor(() => expect(queryClient.isFetching()).toBe(1))

    logout()
    answer(['every invoice in the hospital'])
    await Promise.allSettled([pending])

    expect(queryClient.getQueryData(KEY)).toBeUndefined()
  })

  it('records why a forced sign-out happened, and forgets it at the next sign-in', () => {
    const { setAuth, logout } = useAuthStore.getState()

    setAuth(ADMIN, 'admin-token')
    logout('session_ended')
    expect(useAuthStore.getState().signOutReason).toBe('session_ended')

    setAuth(ADMIN, 'admin-token')
    expect(useAuthStore.getState().signOutReason).toBeNull()

    logout()
    expect(useAuthStore.getState().signOutReason).toBeNull()
  })
})
