import { Link, useLocation } from 'react-router-dom'
import { ChevronRight } from 'lucide-react'
import { usePermissions } from '@/hooks/usePermissions'
import type { Permission } from '@/lib/rbac'

const LABELS: Record<string, string> = {
  dashboard: 'Dashboard',
  patients: 'Patients',
  doctors: 'Doctors',
  appointments: 'Appointments',
  billing: 'Billing',
  laboratory: 'Laboratory',
  catalog: 'Test catalog',
  pharmacy: 'Pharmacy',
  inventory: 'Inventory',
  'purchase-orders': 'Purchase orders',
  reports: 'Reports',
  revenue: 'Revenue',
  outstanding: 'Outstanding',
  settings: 'Settings',
}

/**
 * The paths that have a page of their own in `router.tsx`. A crumb links only
 * to one of these: any other path falls through to the not-found page.
 */
const PAGES = new Set([
  '/dashboard',
  '/patients',
  '/doctors',
  '/appointments',
  '/billing',
  '/laboratory',
  '/laboratory/catalog',
  '/pharmacy',
  '/pharmacy/prescriptions',
  '/pharmacy/medicines',
  '/pharmacy/purchase-orders',
  '/pharmacy/vendors',
  '/inventory',
  '/inventory/stock',
  '/inventory/movements',
  '/inventory/items',
  '/inventory/locations',
  '/inventory/purchase-orders',
  '/reports',
  '/reports/patients',
  '/reports/appointments',
  '/reports/revenue',
  '/reports/outstanding',
  '/users',
  '/settings',
])

/** Path prefixes with no page of their own, and the list their records live on. */
const LIST_FOR: Record<string, string> = {
  // Lab orders are listed on the worklist at /laboratory; /laboratory/orders is not a route.
  '/laboratory/orders': '/laboratory',
}

/**
 * Pages the router mounts behind a permission while a page beneath them is open
 * to everyone: /settings/profile is every user's own, /settings is the
 * administrator's. Without the permission the crumb would bounce to the dashboard.
 */
const REQUIRES: Record<string, Permission> = {
  '/settings': 'settings.read',
}

const RECORD_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

function titleize(seg: string) {
  // A record's id means nothing to the reader; the page itself names the record.
  if (RECORD_ID.test(seg)) return 'Details'
  return LABELS[seg] ?? seg.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Where a crumb for `path` may link, or nothing when no page is mounted there. */
function linkFor(path: string): string | undefined {
  return LIST_FOR[path] ?? (PAGES.has(path) ? path : undefined)
}

/** Route-derived breadcrumb trail. Spec 2C: breadcrumbs on every page. */
export default function Breadcrumbs() {
  const { pathname } = useLocation()
  const { can } = usePermissions()
  const segments = pathname.split('/').filter(Boolean)

  return (
    <nav aria-label="Breadcrumb" className="flex items-center gap-1.5 text-sm">
      {segments.map((seg, i) => {
        const path = '/' + segments.slice(0, i + 1).join('/')
        const isLast = i === segments.length - 1
        const required = REQUIRES[path]
        const to = isLast || (required && !can(required)) ? undefined : linkFor(path)
        return (
          <span key={path} className="flex items-center gap-1.5">
            {i > 0 && <ChevronRight className="text-outline-variant size-3.5" />}
            {isLast ? (
              <span className="font-body text-primary font-semibold">{titleize(seg)}</span>
            ) : to ? (
              <Link to={to} className="font-body text-on-surface-variant hover:text-secondary">
                {titleize(seg)}
              </Link>
            ) : (
              <span className="font-body text-on-surface-variant">{titleize(seg)}</span>
            )}
          </span>
        )
      })}
    </nav>
  )
}
