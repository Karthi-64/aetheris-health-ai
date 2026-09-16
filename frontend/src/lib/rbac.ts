import {
  LayoutDashboard,
  Users,
  Stethoscope,
  CalendarDays,
  Receipt,
  BarChart3,
  Settings as SettingsIcon,
  UserCog,
  type LucideIcon,
} from 'lucide-react'

/** The hospital roles (spec Part 1, 2B). Roles are for DISPLAY only. */
export type Role =
  | 'super_admin'
  | 'hospital_admin'
  | 'receptionist'
  | 'doctor'
  | 'nurse'
  | 'billing_staff'
  | 'lab_technician'

export const ROLE_LABELS: Record<Role, string> = {
  super_admin: 'Super Admin',
  hospital_admin: 'Hospital Admin',
  receptionist: 'Receptionist',
  doctor: 'Doctor',
  nurse: 'Nurse',
  billing_staff: 'Billing Staff',
  lab_technician: 'Lab Technician',
}

/** Reverse lookup: the backend sends role *display names* ("Hospital Admin"). */
export const ROLE_KEY_BY_NAME: Record<string, Role> = Object.fromEntries(
  (Object.entries(ROLE_LABELS) as [Role, string][]).map(([key, label]) => [label, key]),
) as Record<string, Role>

/**
 * Authorization is modelled on PERMISSION CODES issued by the backend
 * (defect F5). The codes below mirror the seeded catalog in
 * `backend/app/seeds/seed.py` — the server checks the same strings via
 * `require_permission()`. The client never derives permissions from a role.
 */
export type Permission =
  // Users & roles (module 02)
  | 'user.read'
  | 'user.create'
  | 'user.update'
  | 'user.deactivate'
  | 'user.reset_password'
  | 'role.read'
  | 'role.assign'
  // Dashboard
  | 'dashboard.view'
  // Patients
  | 'patient.read'
  | 'patient.write'
  // Doctors
  | 'doctor.read'
  | 'doctor.write'
  // Appointments
  | 'appointment.read'
  | 'appointment.write'
  // Billing
  | 'billing.read'
  | 'billing.write'
  // Reports
  | 'report.read'
  // Settings
  | 'settings.read'
  | 'settings.update'
  | 'settings.manage'

/**
 * Fallback role→permission mapping, used ONLY by the dev mock login. In
 * production the permission set arrives from the server in the auth response;
 * the client never derives it from the role. Kept in sync with the seeded
 * system roles in `backend/app/seeds/seed.py`.
 */
export const MOCK_PERMISSIONS_BY_ROLE: Record<Role, Permission[]> = {
  super_admin: [
    'user.read', 'user.create', 'user.update', 'user.deactivate', 'user.reset_password',
    'role.read', 'role.assign', 'dashboard.view',
    'patient.read', 'patient.write', 'doctor.read', 'doctor.write',
    'appointment.read', 'appointment.write', 'billing.read', 'billing.write',
    'report.read', 'settings.read', 'settings.update', 'settings.manage',
  ],
  hospital_admin: [
    'user.read', 'user.create', 'user.update', 'user.deactivate', 'user.reset_password',
    'role.read', 'role.assign', 'dashboard.view',
    'patient.read', 'patient.write', 'doctor.read', 'doctor.write',
    'appointment.read', 'appointment.write', 'billing.read', 'billing.write',
    'report.read', 'settings.read', 'settings.update', 'settings.manage',
  ],
  receptionist: [
    'dashboard.view', 'patient.read', 'patient.write', 'doctor.read',
    'appointment.read', 'appointment.write', 'billing.read',
  ],
  doctor: [
    'dashboard.view', 'patient.read', 'patient.write', 'doctor.read', 'appointment.read',
    'appointment.write', 'billing.read', 'report.read',
  ],
  nurse: ['dashboard.view', 'patient.read', 'patient.write', 'doctor.read', 'appointment.read'],
  billing_staff: ['dashboard.view', 'billing.read', 'billing.write', 'report.read'],
  lab_technician: ['dashboard.view', 'patient.read'],
}

/** Coarse permission groups the nav/routes are expressed in (spec Parts 3–10). */
export type PermissionGroup =
  | 'dashboard.view'
  | 'patient.read'
  | 'doctor.read'
  | 'appointment.read'
  | 'billing.read'
  | 'report.read'
  | 'user.read'
  | 'settings.read'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  /** The permission required to see this nav item and reach its route. */
  permission: PermissionGroup
}

/** Primary sidebar navigation, gated by permission (spec Parts 3–10). */
export const NAV: NavItem[] = [
  { to: '/dashboard', label: 'Dashboard', icon: LayoutDashboard, permission: 'dashboard.view' },
  { to: '/patients', label: 'Patients', icon: Users, permission: 'patient.read' },
  { to: '/doctors', label: 'Doctors', icon: Stethoscope, permission: 'doctor.read' },
  { to: '/appointments', label: 'Appointments', icon: CalendarDays, permission: 'appointment.read' },
  { to: '/billing', label: 'Billing', icon: Receipt, permission: 'billing.read' },
  { to: '/reports', label: 'Reports', icon: BarChart3, permission: 'report.read' },
  { to: '/users', label: 'Users & Roles', icon: UserCog, permission: 'user.read' },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, permission: 'settings.read' },
]

/**
 * Any of these codes satisfies the group — the backend grants fine-grained
 * codes (e.g. `patient.read`) and the legacy mock set used the `.manage`/`.write`
 * coarse forms, so both must keep working.
 */
const GROUP_ALIASES: Record<PermissionGroup, Permission[]> = {
  'dashboard.view': ['dashboard.view'],
  'patient.read': ['patient.read', 'patient.write'],
  'doctor.read': ['doctor.read', 'doctor.write'],
  'appointment.read': ['appointment.read', 'appointment.write'],
  'billing.read': ['billing.read', 'billing.write'],
  'report.read': ['report.read'],
  'user.read': ['user.read'],
  'settings.read': ['settings.read', 'settings.update', 'settings.manage'],
}

export function hasPermission(userPerms: Permission[] | undefined, perm: Permission): boolean {
  return !!userPerms && userPerms.includes(perm)
}

/**
 * True when the user holds ANY permission satisfying the group.
 *
 * The backend catalog has no `dashboard.view` code — the Dashboard is the home
 * page, so any user holding at least one permission may see it (module spec
 * rule 6: a user with NO roles logs in and "sees nothing", i.e. an empty nav).
 */
export function hasAnyPermission(
  userPerms: Permission[] | undefined,
  group: PermissionGroup,
): boolean {
  if (!userPerms || userPerms.length === 0) return false
  if (group === 'dashboard.view') return true
  return GROUP_ALIASES[group].some((p) => userPerms.includes(p))
}

/** Nav items the given permission set may see. */
export function navForPermissions(userPerms: Permission[] | undefined): NavItem[] {
  return NAV.filter((item) => hasAnyPermission(userPerms, item.permission))
}
