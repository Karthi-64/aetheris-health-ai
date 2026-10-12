import { Navigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { usePermissions } from '@/hooks/usePermissions'
import type { Permission, PermissionGroup } from '@/lib/rbac'

/**
 * Route-level authorization (defect F6). Hiding a nav link is only a UX
 * affordance; the route itself must be guarded from the SAME permission the nav
 * filter uses, so typing /billing directly cannot reach a denied module.
 *
 * Pass `group` instead of `permission` for a module that more than one code
 * opens — the nav shows Billing for `invoice.read` or `invoice.read.own`, so
 * the route has to admit both or a doctor's link leads straight back out.
 */
export function RequirePermission({
  permission,
  group,
  children,
}: ({ permission: Permission; group?: never } | { group: PermissionGroup; permission?: never }) & {
  children: ReactNode
}) {
  const { can, canAny } = usePermissions()
  const allowed = group ? canAny(group) : can(permission as Permission)
  if (!allowed) return <Navigate to="/dashboard" replace />
  return <>{children}</>
}
