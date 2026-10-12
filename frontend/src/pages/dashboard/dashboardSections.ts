import type { Permission } from '@/lib/rbac'

/**
 * Which role sections the home page shows. Decided by permission code alone —
 * never by role name — so a custom role gets exactly the sections its codes
 * allow, and each section's request is one the server will accept.
 *
 * Reception and Doctor are personal working views; a holder of
 * `report.admin.read` gets the hospital-wide Admin section in their place (a
 * Hospital Admin holds every report code only so that they can assign them).
 */
export type DashboardSectionName = 'admin' | 'billing' | 'reception' | 'doctor'

export interface DashboardSections {
  admin: boolean
  billing: boolean
  reception: boolean
  doctor: boolean
  /** The user holds at least one `report.*.read` code, shown as a section or not. */
  any: boolean
}

/** Page order: the first one a user is shown also supplies the hospital's "today". */
export const DASHBOARD_SECTION_ORDER: readonly DashboardSectionName[] = [
  'admin',
  'billing',
  'reception',
  'doctor',
]

export function dashboardSectionsFor(can: (permission: Permission) => boolean): DashboardSections {
  const admin = can('report.admin.read')
  const billing = can('report.billing.read')
  const reception = can('report.reception.read')
  const doctor = can('report.doctor.read')
  return {
    admin,
    billing,
    reception: reception && !admin,
    doctor: doctor && !admin,
    any: admin || billing || reception || doctor,
  }
}

/** The first section the user is shown, in page order; undefined when there is none. */
export function firstDashboardSection(sections: DashboardSections): DashboardSectionName | undefined {
  return DASHBOARD_SECTION_ORDER.find((name) => sections[name])
}

/**
 * Grid classes by how many tiles a row holds (static, so Tailwind keeps them).
 * One column on a phone; four and five tiles wrap before they get too narrow
 * for a money amount.
 */
export const TILE_GRID: Record<number, string> = {
  1: 'grid gap-5 sm:grid-cols-1',
  2: 'grid gap-5 sm:grid-cols-2',
  3: 'grid gap-5 sm:grid-cols-3',
  4: 'grid gap-5 sm:grid-cols-2 xl:grid-cols-4',
  5: 'grid gap-5 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-5',
}
