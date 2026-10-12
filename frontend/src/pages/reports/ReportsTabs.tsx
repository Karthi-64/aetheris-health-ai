import { NavLink } from 'react-router-dom'
import { usePermissions } from '@/hooks/usePermissions'
import { cn } from '@/lib/utils'
import { REPORT_SECTIONS, holdsAny } from './reportSections'

/**
 * Sub-navigation across the Reports screens. A report the user's permissions
 * do not cover is not listed: an administrator sees all four, billing staff
 * see Revenue and Outstanding.
 */
export function ReportsTabs() {
  const { can } = usePermissions()
  const sections = REPORT_SECTIONS.filter((s) => holdsAny(can, s.anyOf))
  if (sections.length < 2) return null

  return (
    <nav aria-label="Report sections" className="mb-6 flex flex-wrap gap-2">
      {sections.map((s) => (
        <NavLink
          key={s.to}
          to={s.to}
          // Overview's path starts every other section's path.
          end={s.end}
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
