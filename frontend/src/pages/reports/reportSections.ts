import type { ReportId } from '@/api/reports'
import type { Permission } from '@/lib/rbac'

/**
 * The Reports screens, each with the codes that open it — any one is enough
 * (docs/modules/10-reports-dashboard.md §10): an administrator
 * (`report.admin.read`) reads all four reports, billing staff
 * (`report.billing.read`) the two financial ones. `end` marks a path that is
 * also the prefix of the others, so its tab is active on that exact path only.
 */
export interface ReportSection {
  to: string
  label: string
  anyOf: Permission[]
  end?: boolean
  /** One line for the landing page. The overview has none: it is the landing page. */
  description?: string
}

export const REPORT_SECTIONS: ReportSection[] = [
  { to: '/reports', label: 'Overview', end: true, anyOf: ['report.admin.read', 'report.billing.read'] },
  {
    to: '/reports/patients',
    label: 'Patients',
    anyOf: ['report.admin.read'],
    description: 'Patients registered per day, week or month, and how many are active now.',
  },
  {
    to: '/reports/appointments',
    label: 'Appointments',
    anyOf: ['report.admin.read'],
    description: 'Appointments by status over a period, by doctor and by department.',
  },
  {
    to: '/reports/revenue',
    label: 'Revenue',
    anyOf: ['report.admin.read', 'report.billing.read'],
    description: 'What was billed, collected and refunded over a period, and by payment method.',
  },
  {
    to: '/reports/outstanding',
    label: 'Outstanding',
    anyOf: ['report.admin.read', 'report.billing.read'],
    description: 'Unpaid invoices as of today, by age, oldest first.',
  },
]

/** The codes that let a user read each report; the export needs one of them and `report.export`. */
export const REPORT_READ_CODES: Record<ReportId, Permission[]> = {
  patients: ['report.admin.read'],
  appointments: ['report.admin.read'],
  revenue: ['report.admin.read', 'report.billing.read'],
  outstanding: ['report.admin.read', 'report.billing.read'],
}

/** True when the user holds any of `codes`. */
export function holdsAny(can: (permission: Permission) => boolean, codes: Permission[]): boolean {
  return codes.some((code) => can(code))
}
