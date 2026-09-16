import { describe, it, expect, beforeEach } from 'vitest'
import { toAuthUser, useAuthStore } from './auth-store'

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
    const user = toAuthUser({ id: 'u4', email: 'a@b.test', first_name: 'A', last_name: 'B', roles: ['Pharmacist'] })
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
