import type { UserStatus } from '@/api/users'

/** Display labels for the backend user statuses. */
export const USER_STATUS_LABELS: Record<string, string> = {
  active: 'Active',
  invited: 'Invited',
  suspended: 'Suspended',
  deactivated: 'Deactivated',
}

/** Badge variants per status, matching the Clinical Glass palette. */
export const USER_STATUS_VARIANT: Record<string, 'success' | 'accent' | 'warning' | 'neutral'> = {
  active: 'success',
  invited: 'accent',
  suspended: 'warning',
  deactivated: 'neutral',
}

/** "Priya Sharma" from { first_name, last_name }. */
export function userFullName(user: { first_name: string; last_name: string }): string {
  return `${user.first_name} ${user.last_name}`.trim()
}

export type { UserStatus }
