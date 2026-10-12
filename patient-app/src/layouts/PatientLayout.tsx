import { Building2, CalendarDays, Hospital, House, type LucideIcon } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { cn } from '@atheris/ui'
import { Brand } from '@/components/Brand'

/** `end`: current only on that exact path. Hospitals and Appointments stay current on the pages under them. */
const NAV: { to: string; label: string; icon: LucideIcon; end: boolean }[] = [
  { to: '/', label: 'Home', icon: House, end: true },
  { to: '/hospitals', label: 'Hospitals', icon: Hospital, end: false },
  { to: '/appointments', label: 'Appointments', icon: CalendarDays, end: false },
  { to: '/link-patient', label: 'Link hospital', icon: Building2, end: true },
]

/**
 * Mobile-first shell for the signed-in app: a quiet header, one column of
 * content, and bottom navigation within thumb reach.
 */
export function PatientLayout() {
  return (
    <div className="bg-background flex min-h-dvh flex-col">
      <header className="bg-background/95 sticky top-0 z-10 border-b">
        <div className="mx-auto flex h-14 w-full max-w-xl items-center px-4">
          <Brand />
        </div>
      </header>

      <main className="mx-auto w-full max-w-xl flex-1 px-4 pt-6 pb-28">
        <Outlet />
      </main>

      <nav
        aria-label="Main"
        className="bg-surface-container-lowest fixed inset-x-0 bottom-0 z-10 border-t pb-[env(safe-area-inset-bottom)]"
      >
        <ul className="mx-auto flex w-full max-w-xl">
          {NAV.map(({ to, label, icon: Icon, end }) => (
            <li key={to} className="flex-1">
              <NavLink
                to={to}
                end={end}
                className={({ isActive }) =>
                  cn(
                    'text-body-sm flex min-h-14 flex-col items-center justify-center gap-0.5 px-1 text-center leading-tight font-medium',
                    isActive ? 'text-secondary' : 'text-on-surface-variant hover:text-on-surface',
                  )
                }
              >
                <Icon className="size-5" aria-hidden />
                {label}
              </NavLink>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  )
}
