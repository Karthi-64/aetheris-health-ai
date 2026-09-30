import { describe, it, expect } from 'vitest'
import {
  hasPermission,
  hasAnyPermission,
  navForPermissions,
  MOCK_PERMISSIONS_BY_ROLE,
  ROLE_KEY_BY_NAME,
  type Permission,
} from './rbac'

describe('rbac', () => {
  it('hasPermission checks membership in the permission set', () => {
    const perms: Permission[] = ['dashboard.view', 'patient.read']
    expect(hasPermission(perms, 'patient.read')).toBe(true)
    expect(hasPermission(perms, 'billing.read')).toBe(false)
    expect(hasPermission(undefined, 'dashboard.view')).toBe(false)
  })

  it('hasAnyPermission gates a group on its seeded read code', () => {
    expect(hasAnyPermission(['patient.read'], 'patient.read')).toBe(true)
    // Being able to create patients does not imply the list view: the server
    // requires patient.read for GET /patients, so the nav must not promise it.
    expect(hasAnyPermission(['patient.create'], 'patient.read')).toBe(false)
    expect(hasAnyPermission(['report.read'], 'patient.read')).toBe(false)
    expect(hasAnyPermission(undefined, 'dashboard.view')).toBe(false)
  })

  it('the dashboard is the home page — any permission holder sees it, the permission-less do not', () => {
    // The backend catalog has no dashboard.view code.
    expect(hasAnyPermission(['patient.read'], 'dashboard.view')).toBe(true)
    expect(hasAnyPermission(['user.read'], 'dashboard.view')).toBe(true)
    expect(hasAnyPermission([], 'dashboard.view')).toBe(false)
    expect(navForPermissions([])).toHaveLength(0)
    expect(navForPermissions(['patient.read']).map((n) => n.to)).toContain('/dashboard')
  })

  it('navForPermissions only returns items the user is permitted to see', () => {
    // Receptionist has no report/settings permission.
    const nav = navForPermissions(MOCK_PERMISSIONS_BY_ROLE.receptionist)
    const paths = nav.map((n) => n.to)
    expect(paths).toContain('/patients')
    expect(paths).toContain('/appointments')
    expect(paths).not.toContain('/reports')
    expect(paths).not.toContain('/settings')
    expect(paths).not.toContain('/users')
  })

  it('an admin sees the Users & Roles nav item', () => {
    const paths = navForPermissions(MOCK_PERMISSIONS_BY_ROLE.hospital_admin).map((n) => n.to)
    expect(paths).toContain('/users')
    expect(paths).toContain('/settings')
  })

  it('billing staff sees billing and reports but not settings', () => {
    // Seed grant: Billing Staff holds patient.read (invoice context) but no
    // settings or user-management permissions.
    const paths = navForPermissions(MOCK_PERMISSIONS_BY_ROLE.billing_staff).map((n) => n.to)
    expect(paths).toEqual(expect.arrayContaining(['/dashboard', '/billing', '/reports', '/patients']))
    expect(paths).not.toContain('/settings')
    expect(paths).not.toContain('/users')
  })

  it('an empty permission set sees no nav', () => {
    expect(navForPermissions([])).toHaveLength(0)
    expect(navForPermissions(undefined)).toHaveLength(0)
  })

  it('maps backend role display names to local keys', () => {
    expect(ROLE_KEY_BY_NAME['Hospital Admin']).toBe('hospital_admin')
    expect(ROLE_KEY_BY_NAME['Doctor']).toBe('doctor')
    expect(ROLE_KEY_BY_NAME['Unknown Role']).toBeUndefined()
  })
})
