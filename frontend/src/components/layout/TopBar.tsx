import { Menu } from 'lucide-react'
import Breadcrumbs from '@/components/layout/Breadcrumbs'
import { ThemeToggle } from '@/components/layout/ThemeToggle'
import { NotificationBell } from '@/components/notifications/NotificationBell'
import { useAuthStore } from '@/store/auth-store'
import { ROLE_LABELS } from '@/lib/rbac'

function initials(name: string) {
  const parts = name.trim().split(/\s+/)
  return parts.slice(-2).map((p) => p[0]).join('').toUpperCase()
}

interface TopBarProps {
  onOpenSidebar: () => void
}

/** App top bar: breadcrumbs, theme, notifications, who is signed in (spec 2C §2, §6). */
export default function TopBar({ onOpenSidebar }: TopBarProps) {
  const user = useAuthStore((s) => s.user)
  const name = user?.name ?? 'User'
  const roleLabel = user?.role ? ROLE_LABELS[user.role] : ''

  return (
    <header className="glassmorphism shadow-glass-panel sticky top-0 z-30 flex h-16 items-center gap-3 rounded-2xl px-3 md:px-4">
      {/* Mobile menu */}
      <button
        onClick={onOpenSidebar}
        aria-label="Open menu"
        className="text-primary hover:bg-white/30 flex size-10 items-center justify-center rounded-lg lg:hidden"
      >
        <Menu className="size-5" />
      </button>

      {/* Breadcrumbs (desktop) */}
      <div className="hidden md:block">
        <Breadcrumbs />
      </div>

      {/* Actions — pushed to the right edge at every width */}
      <div className="ml-auto flex items-center gap-1.5">
        <ThemeToggle />

        <NotificationBell />

        {/* User identity — display only; logout lives in the sidebar (F10) */}
        <div className="flex items-center gap-2 py-1 pr-1 pl-2">
          <div className="hidden text-right leading-tight md:block">
            <p className="font-body text-body-sm text-primary font-bold">{name}</p>
            <p className="font-body text-outline text-[11px]">{roleLabel}</p>
          </div>
          <span className="neo-extruded bg-primary-container flex size-9 items-center justify-center rounded-full text-xs font-bold text-white">
            {initials(name)}
          </span>
        </div>
      </div>
    </header>
  )
}
