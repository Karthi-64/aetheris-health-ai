import {
  Boxes,
  FlaskConical,
  Pill,
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
  | 'pharmacist'
  | 'inventory_manager'

export const ROLE_LABELS: Record<Role, string> = {
  super_admin: 'Super Admin',
  hospital_admin: 'Hospital Admin',
  receptionist: 'Receptionist',
  doctor: 'Doctor',
  nurse: 'Nurse',
  billing_staff: 'Billing Staff',
  lab_technician: 'Lab Technician',
  pharmacist: 'Pharmacist',
  inventory_manager: 'Inventory Manager',
}

/** Reverse lookup: the backend sends role *display names* ("Hospital Admin"). */
export const ROLE_KEY_BY_NAME: Record<string, Role> = Object.fromEntries(
  (Object.entries(ROLE_LABELS) as [Role, string][]).map(([key, label]) => [label, key]),
) as Record<string, Role>

/**
 * Authorization is modelled on PERMISSION CODES issued by the backend
 * (defect F5). This union mirrors the seeded catalog in
 * `backend/app/seeds/seed.py` **exactly** — parity is enforced by
 * `backend/app/tests/unit/test_frontend_permission_parity.py`, so a code
 * renamed on either side fails CI instead of silently dead-locking the UI.
 *
 * `dashboard.view` is the one client-only pseudo-code: the backend has no
 * such permission because the Dashboard is the home page — any user holding
 * at least one permission may see it.
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
  // Patients
  | 'patient.read'
  | 'patient.create'
  | 'patient.update'
  | 'patient.delete'
  // Doctors
  | 'doctor.read'
  | 'doctor.create'
  | 'doctor.update'
  | 'doctor.delete'
  | 'doctor.availability.read'
  | 'doctor.availability.update'
  | 'doctor.leave.create'
  | 'doctor.leave.delete'
  // Appointments
  | 'appointment.read'
  | 'appointment.read.own'
  | 'appointment.book'
  | 'appointment.reschedule'
  | 'appointment.cancel'
  | 'appointment.check_in'
  | 'appointment.start'
  | 'appointment.complete'
  | 'appointment.book_override'
  | 'appointment.recommend_slot'
  // Billing (docs/modules/06-billing.md §10)
  | 'service.read'
  | 'service.create'
  | 'service.update'
  | 'invoice.read'
  | 'invoice.read.own'
  | 'invoice.create'
  | 'invoice.update'
  | 'invoice.issue'
  | 'invoice.void'
  | 'invoice.approve_discount'
  | 'invoice.payment.record'
  | 'invoice.payment.record.cash'
  | 'invoice.refund'
  | 'invoice.pdf.download'
  | 'invoice.ai_explain'
  // Notifications (docs/modules/11-notifications.md §10)
  | 'notification.read.own'
  | 'notification.preference.update.own'
  | 'notification.broadcast'
  | 'notification.template.read'
  | 'notification.template.create'
  | 'notification.template.update'
  | 'notification.delivery.read'
  // Laboratory
  | 'lab.test.read'
  | 'lab.test.create'
  | 'lab.test.update'
  | 'lab.order.read'
  | 'lab.order.create'
  | 'lab.order.cancel'
  | 'lab.order.collect_sample'
  | 'lab.order.enter_results'
  | 'lab.order.release'
  | 'lab.order.amend'
  | 'lab.report.download'
  | 'lab.ai_explain'
  // Pharmacy
  | 'pharmacy.medicine.read'
  | 'pharmacy.medicine.create'
  | 'pharmacy.medicine.update'
  | 'pharmacy.batch.read'
  | 'pharmacy.batch.create'
  | 'pharmacy.batch.update'
  | 'pharmacy.prescription.read'
  | 'pharmacy.prescription.create'
  | 'pharmacy.dispense.execute'
  | 'pharmacy.interaction.check'
  | 'pharmacy.po.read'
  | 'pharmacy.po.create'
  | 'pharmacy.po.update'
  | 'pharmacy.po.receive'
  | 'pharmacy.vendor.read'
  | 'pharmacy.vendor.create'
  | 'pharmacy.vendor.update'
  | 'pharmacy.ai_substitute'
  // Inventory
  | 'inventory.item.read'
  | 'inventory.item.create'
  | 'inventory.item.update'
  | 'inventory.location.read'
  | 'inventory.location.create'
  | 'inventory.location.update'
  | 'inventory.stock.read'
  | 'inventory.consume'
  | 'inventory.transfer'
  | 'inventory.adjust'
  | 'inventory.po.read'
  | 'inventory.po.create'
  | 'inventory.po.update'
  | 'inventory.po.receive'
  | 'inventory.forecast.read'
  // Reports (docs/modules/10-reports-dashboard.md §10): one read code per role
  // dashboard; the admin and billing codes also open reports.
  | 'report.admin.read'
  | 'report.doctor.read'
  | 'report.reception.read'
  | 'report.billing.read'
  | 'report.export'
  // Settings & departments
  | 'settings.read'
  | 'settings.update'
  | 'department.read'
  | 'department.create'
  | 'department.update'
  | 'department.delete'
  // Audit
  | 'audit.read'
  | 'audit.export'
  // Client-only pseudo-code (see docstring above)
  | 'dashboard.view'

/**
 * Fallback role→permission mapping, used ONLY by the dev mock login when the
 * API is unreachable. The codes mirror the seeded system roles in
 * `backend/app/seeds/seed.py`; in production the permission set arrives from
 * the server in the auth response and the client never derives it from the
 * role.
 */
export const MOCK_PERMISSIONS_BY_ROLE: Record<Role, Permission[]> = {
  super_admin: [
    'notification.read.own', 'notification.preference.update.own',
    'notification.broadcast', 'notification.template.read', 'notification.template.create',
    'notification.template.update', 'notification.delivery.read',
    'user.read', 'user.create', 'user.update', 'user.deactivate', 'user.reset_password',
    'role.read', 'role.assign', 'dashboard.view',
    'patient.read', 'patient.create', 'patient.update', 'patient.delete',
    'doctor.read', 'doctor.create', 'doctor.update', 'doctor.delete',
    'doctor.availability.read', 'doctor.availability.update',
    'doctor.leave.create', 'doctor.leave.delete',
    'appointment.read', 'appointment.read.own', 'appointment.book', 'appointment.reschedule',
    'appointment.cancel', 'appointment.check_in', 'appointment.start', 'appointment.complete',
    'appointment.book_override', 'appointment.recommend_slot',
    'service.read', 'service.create', 'service.update',
    'invoice.read', 'invoice.read.own', 'invoice.create', 'invoice.update', 'invoice.issue',
    'invoice.void', 'invoice.approve_discount', 'invoice.payment.record',
    'invoice.payment.record.cash', 'invoice.refund', 'invoice.pdf.download', 'invoice.ai_explain',
    'lab.test.read', 'lab.test.create', 'lab.test.update',
    'lab.order.read', 'lab.order.create', 'lab.order.cancel', 'lab.order.collect_sample',
    'lab.order.enter_results', 'lab.order.release', 'lab.order.amend',
    'lab.report.download', 'lab.ai_explain',
    'pharmacy.medicine.read', 'pharmacy.medicine.create', 'pharmacy.medicine.update',
    'pharmacy.batch.read', 'pharmacy.batch.create', 'pharmacy.batch.update',
    'pharmacy.prescription.read', 'pharmacy.prescription.create', 'pharmacy.dispense.execute',
    'pharmacy.interaction.check',
    'pharmacy.po.read', 'pharmacy.po.create', 'pharmacy.po.update', 'pharmacy.po.receive',
    'pharmacy.vendor.read', 'pharmacy.vendor.create', 'pharmacy.vendor.update',
    'pharmacy.ai_substitute',
    'inventory.item.read', 'inventory.item.create', 'inventory.item.update',
    'inventory.location.read', 'inventory.location.create', 'inventory.location.update',
    'inventory.stock.read', 'inventory.consume', 'inventory.transfer', 'inventory.adjust',
    'inventory.po.read', 'inventory.po.create', 'inventory.po.update', 'inventory.po.receive',
    'inventory.forecast.read',
    'report.admin.read', 'report.doctor.read', 'report.reception.read', 'report.billing.read',
    'report.export',
    'settings.read', 'settings.update',
    'department.read', 'department.create', 'department.update', 'department.delete',
    'audit.read', 'audit.export',
  ],
  hospital_admin: [
    'notification.read.own', 'notification.preference.update.own',
    'notification.broadcast', 'notification.template.read', 'notification.template.create',
    'notification.template.update', 'notification.delivery.read',
    'user.read', 'user.create', 'user.update', 'user.deactivate', 'user.reset_password',
    'role.read', 'role.assign', 'dashboard.view',
    'patient.read', 'patient.create', 'patient.update', 'patient.delete',
    'doctor.read', 'doctor.create', 'doctor.update', 'doctor.delete',
    'doctor.availability.read', 'doctor.availability.update',
    'doctor.leave.create', 'doctor.leave.delete',
    'appointment.read', 'appointment.read.own', 'appointment.book', 'appointment.reschedule',
    'appointment.cancel', 'appointment.check_in', 'appointment.start', 'appointment.complete',
    'appointment.book_override', 'appointment.recommend_slot',
    'service.read', 'service.create', 'service.update',
    'invoice.read', 'invoice.read.own', 'invoice.create', 'invoice.update', 'invoice.issue',
    'invoice.void', 'invoice.approve_discount', 'invoice.payment.record',
    'invoice.payment.record.cash', 'invoice.refund', 'invoice.pdf.download', 'invoice.ai_explain',
    'lab.test.read', 'lab.test.create', 'lab.test.update',
    'lab.order.read', 'lab.order.create', 'lab.order.cancel', 'lab.order.collect_sample',
    'lab.order.enter_results', 'lab.order.release', 'lab.order.amend',
    'lab.report.download', 'lab.ai_explain',
    'pharmacy.medicine.read', 'pharmacy.medicine.create', 'pharmacy.medicine.update',
    'pharmacy.batch.read', 'pharmacy.batch.create', 'pharmacy.batch.update',
    'pharmacy.prescription.read', 'pharmacy.prescription.create', 'pharmacy.dispense.execute',
    'pharmacy.interaction.check',
    'pharmacy.po.read', 'pharmacy.po.create', 'pharmacy.po.update', 'pharmacy.po.receive',
    'pharmacy.vendor.read', 'pharmacy.vendor.create', 'pharmacy.vendor.update',
    'pharmacy.ai_substitute',
    'inventory.item.read', 'inventory.item.create', 'inventory.item.update',
    'inventory.location.read', 'inventory.location.create', 'inventory.location.update',
    'inventory.stock.read', 'inventory.consume', 'inventory.transfer', 'inventory.adjust',
    'inventory.po.read', 'inventory.po.create', 'inventory.po.update', 'inventory.po.receive',
    'inventory.forecast.read',
    'report.admin.read', 'report.doctor.read', 'report.reception.read', 'report.billing.read',
    'report.export',
    'settings.read', 'settings.update',
    'department.read', 'department.create', 'department.update', 'department.delete',
    'audit.read',
  ],
  receptionist: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    'patient.read', 'patient.create',
    'appointment.read', 'appointment.book', 'appointment.reschedule', 'appointment.cancel',
    'appointment.check_in', 'appointment.recommend_slot',
    // Module spec 06 §3: a receptionist views invoices and records cash payments.
    'service.read', 'invoice.read', 'invoice.payment.record.cash',
    // The reception dashboard only: no report and no money figure.
    'report.reception.read',
    'department.read', 'doctor.read', 'doctor.availability.read',
  ],
  doctor: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    'patient.read', 'patient.create', 'patient.update',
    'appointment.read', 'appointment.read.own', 'appointment.start', 'appointment.complete',
    'appointment.check_in',
    // Module spec 06 §3: a doctor sees the invoices for their own visits only.
    // The server applies that scope; the client just shows what it returns.
    'invoice.read.own',
    'lab.test.read', 'lab.order.read', 'lab.order.create', 'lab.order.cancel',
    'pharmacy.medicine.read', 'pharmacy.prescription.read', 'pharmacy.prescription.create',
    // The doctor's own dashboard only; it opens no report.
    'report.doctor.read',
    'department.read', 'doctor.read', 'doctor.availability.read', 'doctor.availability.update',
    'doctor.leave.create', 'doctor.leave.delete',
  ],
  nurse: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    'patient.read', 'patient.update',
    'appointment.read', 'appointment.check_in',
    'lab.order.read',
    // Module spec 09 §3: ward staff view local stock and record what they use.
    'inventory.item.read', 'inventory.location.read', 'inventory.stock.read', 'inventory.consume',
    'department.read', 'doctor.read', 'doctor.availability.read',
  ],
  billing_staff: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    'patient.read',
    // No `invoice.void`: voiding is an admin action (module spec 06 §4, rule 4).
    'service.read', 'invoice.read', 'invoice.create', 'invoice.update', 'invoice.issue',
    'invoice.payment.record',
    'report.billing.read', 'report.export', 'department.read', 'doctor.read',
  ],
  lab_technician: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    // No `lab.order.release`: release is a supervisor step (module spec 07 §3).
    'lab.test.read', 'lab.order.read', 'lab.order.collect_sample', 'lab.order.enter_results',
    'department.read',
  ],
  pharmacist: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    // Dispenses and takes stock in, but cannot change the catalog, prescribe
    // or raise a purchase order (docs/18-API_CONTRACTS.md §9.2).
    'pharmacy.medicine.read',
    'pharmacy.batch.read', 'pharmacy.batch.create', 'pharmacy.batch.update',
    'pharmacy.prescription.read', 'pharmacy.dispense.execute',
    'pharmacy.po.read', 'pharmacy.po.receive', 'pharmacy.vendor.read',
    'inventory.item.read', 'inventory.location.read', 'inventory.stock.read',
    'department.read',
  ],
  inventory_manager: [
    'notification.read.own', 'notification.preference.update.own',
    'dashboard.view',
    // Owns purchase orders and vendors; sees stock but no prescriptions.
    'pharmacy.medicine.read', 'pharmacy.batch.read',
    'pharmacy.po.read', 'pharmacy.po.create', 'pharmacy.po.update', 'pharmacy.po.receive',
    'pharmacy.vendor.read', 'pharmacy.vendor.create', 'pharmacy.vendor.update',
    'inventory.item.read', 'inventory.item.create', 'inventory.item.update',
    'inventory.location.read', 'inventory.location.create', 'inventory.location.update',
    'inventory.stock.read', 'inventory.consume', 'inventory.transfer', 'inventory.adjust',
    'inventory.po.read', 'inventory.po.create', 'inventory.po.update', 'inventory.po.receive',
    'inventory.forecast.read',
    'department.read',
  ],
}

/** Coarse permission groups the nav/routes are expressed in (spec Parts 3–10). */
export type PermissionGroup =
  | 'dashboard.view'
  | 'patient.read'
  | 'doctor.read'
  | 'appointment.read'
  | 'invoice.read'
  | 'lab.order.read'
  | 'pharmacy.medicine.read'
  | 'inventory.stock.read'
  | 'report.admin.read'
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
  { to: '/billing', label: 'Billing', icon: Receipt, permission: 'invoice.read' },
  { to: '/laboratory', label: 'Laboratory', icon: FlaskConical, permission: 'lab.order.read' },
  { to: '/pharmacy', label: 'Pharmacy', icon: Pill, permission: 'pharmacy.medicine.read' },
  { to: '/inventory', label: 'Inventory', icon: Boxes, permission: 'inventory.stock.read' },
  { to: '/reports', label: 'Reports', icon: BarChart3, permission: 'report.admin.read' },
  { to: '/users', label: 'Users & Roles', icon: UserCog, permission: 'user.read' },
  { to: '/settings', label: 'Settings', icon: SettingsIcon, permission: 'settings.read' },
]

/**
 * The server code that satisfies each nav group. Client-side navigation is
 * expressed in these coarse groups for readability, but every alias is a real
 * seeded code — the parity test forbids invented ones.
 */
const GROUP_ALIASES: Record<PermissionGroup, Permission[]> = {
  'dashboard.view': ['dashboard.view'],
  'patient.read': ['patient.read'],
  'doctor.read': ['doctor.read'],
  'appointment.read': ['appointment.read'],
  // Either read code opens the Billing module: every billing screen starts
  // from the invoice list, and the server narrows that list for `.own`.
  'invoice.read': ['invoice.read', 'invoice.read.own'],
  // The worklist is the module's first screen; every lab role can read orders.
  'lab.order.read': ['lab.order.read'],
  // Every pharmacy role reads the catalog; the others cover a custom role
  // that was given one pharmacy screen without it.
  'pharmacy.medicine.read': [
    'pharmacy.medicine.read',
    'pharmacy.prescription.read',
    'pharmacy.po.read',
    'pharmacy.vendor.read',
  ],
  // Every inventory role reads stock; the others cover a custom role that was
  // given one inventory screen without it.
  'inventory.stock.read': [
    'inventory.stock.read',
    'inventory.item.read',
    'inventory.location.read',
    'inventory.po.read',
  ],
  // Only these two codes open a report. Doctors and receptionists hold a
  // dashboard code (`report.doctor.read`, `report.reception.read`) and have
  // dashboards only, so neither code is an alias here.
  'report.admin.read': ['report.admin.read', 'report.billing.read'],
  'user.read': ['user.read'],
  'settings.read': ['settings.read'],
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
