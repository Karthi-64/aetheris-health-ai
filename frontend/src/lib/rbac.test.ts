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
    expect(hasPermission(perms, 'invoice.read')).toBe(false)
    expect(hasPermission(undefined, 'dashboard.view')).toBe(false)
  })

  it('hasAnyPermission gates a group on its seeded read code', () => {
    expect(hasAnyPermission(['patient.read'], 'patient.read')).toBe(true)
    // Being able to create patients does not imply the list view: the server
    // requires patient.read for GET /patients, so the nav must not promise it.
    expect(hasAnyPermission(['patient.create'], 'patient.read')).toBe(false)
    expect(hasAnyPermission(['report.admin.read'], 'patient.read')).toBe(false)
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

  it('reports are opened by the admin or the billing read code, not by a dashboard-only code', () => {
    // docs/modules/10-reports-dashboard.md §10: doctors and receptionists have
    // a dashboard and no report.
    expect(hasAnyPermission(['report.admin.read'], 'report.admin.read')).toBe(true)
    expect(hasAnyPermission(['report.billing.read'], 'report.admin.read')).toBe(true)
    expect(hasAnyPermission(['report.doctor.read'], 'report.admin.read')).toBe(false)
    expect(hasAnyPermission(['report.reception.read'], 'report.admin.read')).toBe(false)
    // Exporting alone opens nothing: there is no report to export from.
    expect(hasAnyPermission(['report.export'], 'report.admin.read')).toBe(false)
    expect(navForPermissions(MOCK_PERMISSIONS_BY_ROLE.doctor).map((n) => n.to)).not.toContain('/reports')
    expect(navForPermissions(MOCK_PERMISSIONS_BY_ROLE.hospital_admin).map((n) => n.to)).toContain('/reports')
  })

  it('each role holds exactly its seeded report codes', () => {
    const reportCodes = (role: keyof typeof MOCK_PERMISSIONS_BY_ROLE) =>
      MOCK_PERMISSIONS_BY_ROLE[role].filter((p) => p.startsWith('report.')).sort()
    const all = [
      'report.admin.read',
      'report.billing.read',
      'report.doctor.read',
      'report.export',
      'report.reception.read',
    ]
    expect(reportCodes('super_admin')).toEqual(all)
    expect(reportCodes('hospital_admin')).toEqual(all)
    expect(reportCodes('doctor')).toEqual(['report.doctor.read'])
    expect(reportCodes('receptionist')).toEqual(['report.reception.read'])
    expect(reportCodes('billing_staff')).toEqual(['report.billing.read', 'report.export'])
    for (const role of ['nurse', 'lab_technician', 'pharmacist', 'inventory_manager'] as const) {
      expect(reportCodes(role)).toEqual([])
    }
  })

  it('billing is opened by either invoice read code', () => {
    // Mirrors the seeded roles: docs/modules/06-billing.md §3 and §10.
    expect(hasAnyPermission(['invoice.read'], 'invoice.read')).toBe(true)
    // A doctor holds only the narrow code; the server scopes what they see.
    expect(hasAnyPermission(['invoice.read.own'], 'invoice.read')).toBe(true)
    // Recording payments alone does not open the module — there is no list to open.
    expect(hasAnyPermission(['invoice.payment.record.cash'], 'invoice.read')).toBe(false)
    expect(navForPermissions(MOCK_PERMISSIONS_BY_ROLE.receptionist).map((n) => n.to)).toContain('/billing')
    expect(navForPermissions(MOCK_PERMISSIONS_BY_ROLE.doctor).map((n) => n.to)).toContain('/billing')
    expect(navForPermissions(MOCK_PERMISSIONS_BY_ROLE.nurse).map((n) => n.to)).not.toContain('/billing')
  })

  it('a receptionist holds the cash-only payment code, not the full one', () => {
    const perms = MOCK_PERMISSIONS_BY_ROLE.receptionist
    expect(hasPermission(perms, 'invoice.payment.record.cash')).toBe(true)
    expect(hasPermission(perms, 'invoice.payment.record')).toBe(false)
    expect(hasPermission(MOCK_PERMISSIONS_BY_ROLE.billing_staff, 'invoice.payment.record')).toBe(true)
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
