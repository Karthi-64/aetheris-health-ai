import { NavLink } from 'react-router-dom'
import { usePermissions } from '@/hooks/usePermissions'
import { cn } from '@/lib/utils'
import { PHARMACY_SECTIONS } from './pharmacySections'

/**
 * Sub-navigation across the Pharmacy screens. A section the user's
 * permissions do not cover is not listed: a doctor sees prescriptions and the
 * catalog, an inventory manager sees the catalog, orders and vendors.
 */
export function PharmacyTabs() {
  const { can } = usePermissions()
  const sections = PHARMACY_SECTIONS.filter((s) => can(s.permission))
  if (sections.length < 2) return null

  return (
    <nav aria-label="Pharmacy sections" className="mb-6 flex flex-wrap gap-2">
      {sections.map((s) => (
        <NavLink
          key={s.to}
          to={s.to}
          className={({ isActive }) =>
            cn(
              'font-body text-body-sm focus-visible:ring-secondary rounded-full px-4 py-2 transition-colors outline-none focus-visible:ring-2',
              isActive
                ? 'bg-secondary-container text-on-secondary-container font-semibold'
                : 'neo-pressed bg-surface text-on-surface-variant hover:text-secondary',
            )
          }
        >
          {s.label}
        </NavLink>
      ))}
    </nav>
  )
}
