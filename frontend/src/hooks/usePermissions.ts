import { useAuthStore } from '@/store/auth-store'
import {
  hasAnyPermission,
  hasPermission,
  navForPermissions,
  type Permission,
  type PermissionGroup,
} from '@/lib/rbac'

/**
 * Authorization for the current user, modelled on PERMISSION CODES (defect F5).
 * Components ask `can('patient.read')` — never `hasRole(...)`. Roles are display
 * only. This is a UX affordance, not a security boundary: the backend enforces.
 *
 * The store keeps `permissions` as `string[]` (the server is the source of
 * truth and may issue codes this build doesn't know yet), so helpers accept
 * the string form directly.
 */
export function usePermissions() {
  const user = useAuthStore((s) => s.user)
  const permissions = user?.permissions

  return {
    role: user?.role,
    can: (permission: Permission) => hasPermission(permissions as Permission[] | undefined, permission),
    /** True when the user holds any code satisfying the coarse group. */
    canAny: (group: PermissionGroup) => hasAnyPermission(permissions as Permission[] | undefined, group),
    /** Nav items the current permission set may see. */
    nav: navForPermissions(permissions as Permission[] | undefined),
  }
}
