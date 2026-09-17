import { Link, NavLink, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { ChevronsLeft, ChevronsRight, LogOut, Sparkles } from 'lucide-react'
import { Logo } from '@/components/brand/Logo'
import { Sheet, SheetContent, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { useAuthStore } from '@/store/auth-store'
import { usePermissions } from '@/hooks/usePermissions'
import { ROLE_LABELS } from '@/lib/rbac'
import { api } from '@/lib/api'
import { tokenStore } from '@/services/tokenStore'
import { cn } from '@/lib/utils'

function initials(name: string) {
  const parts = name.trim().split(/\s+/)
  return parts.slice(-2).map((p) => p[0]).join('').toUpperCase()
}

interface SidebarProps {
  collapsed: boolean
  onToggleCollapse: () => void
  mobileOpen: boolean
  onMobileClose: () => void
  onOpenCopilot: () => void
}

interface SidebarBodyProps {
  collapsed: boolean
  onToggleCollapse?: () => void
  onNavigate?: () => void
  onOpenCopilot: () => void
}

/** Shared inner content — rendered in the persistent desktop rail and the mobile drawer. */
function SidebarBody({ collapsed, onToggleCollapse, onNavigate, onOpenCopilot }: SidebarBodyProps) {
  const navigate = useNavigate()
  const { nav, role } = usePermissions()
  const user = useAuthStore((s) => s.user)
  const logout = useAuthStore((s) => s.logout)

  const name = user?.name ?? 'User'
  const roleLabel = role ? ROLE_LABELS[role] : ''

  async function handleLogout() {
    // Attempt to revoke the refresh token on the backend. If the network is
    // unavailable, we still clear the local session (task 4 — graceful
    // offline logout).
    const currentRefresh = tokenStore.getRefreshToken()
    if (currentRefresh) {
      try {
        await api.post('/auth/logout', { refresh_token: currentRefresh })
      } catch {
        // Network error — proceed with local cleanup anyway.
      }
    }
    logout()
    toast.success('Signed out')
    navigate('/login', { replace: true })
  }

  return (
    <>
      {/* Brand */}
      <div className="mb-4 flex items-center justify-between px-1 pt-1">
        <Link to="/dashboard" className="flex items-center overflow-hidden" onClick={onNavigate}>
          <Logo variant={collapsed ? 'mark' : 'lockup'} className={collapsed ? 'h-9' : 'h-10'} />
        </Link>
        {onToggleCollapse && (
          <button
            onClick={onToggleCollapse}
            aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            className="text-outline hover:text-primary hidden rounded-lg p-1 transition-colors lg:block"
          >
            {collapsed ? <ChevronsRight className="size-4" /> : <ChevronsLeft className="size-4" />}
          </button>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-1 overflow-y-auto">
        {nav.map(({ to, label, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            onClick={onNavigate}
            className={({ isActive }) =>
              cn(
                'flex items-center gap-3 rounded-xl px-3 py-2.5 transition-all duration-200',
                collapsed && 'justify-center',
                isActive
                  ? 'neo-pressed text-secondary font-bold'
                  : 'text-on-surface-variant hover:text-primary hover:translate-x-0.5',
              )
            }
            title={collapsed ? label : undefined}
          >
            <Icon className="size-5 shrink-0" />
            {!collapsed && <span className="font-body text-body-md truncate">{label}</span>}
          </NavLink>
        ))}
      </nav>

      {/* AI Copilot launcher */}
      <button
        onClick={onOpenCopilot}
        className={cn(
          'neo-extruded bg-primary-container flex items-center gap-3 rounded-xl px-3 py-2.5 font-bold text-white transition-transform active:scale-[0.98]',
          collapsed && 'justify-center',
        )}
        title="AI Copilot"
      >
        <Sparkles className="text-secondary-container size-5 shrink-0" />
        {!collapsed && <span className="font-label text-label-caps">AI Copilot</span>}
      </button>

      {/* User — the identity block is the way into the own-profile page, which
          is available to every authenticated user regardless of permissions. */}
      <div className="border-outline-variant/30 mt-2 flex items-center gap-3 border-t pt-3">
        <Link
          to="/settings/profile"
          onClick={onNavigate}
          title="My profile"
          className="neo-extruded bg-primary-container flex size-10 shrink-0 items-center justify-center rounded-full text-sm font-bold text-white transition-transform active:scale-95"
        >
          {initials(name)}
        </Link>
        {!collapsed && (
          <>
            <Link
              to="/settings/profile"
              onClick={onNavigate}
              className="min-w-0 flex-1 text-left"
            >
              <p className="font-body text-body-sm text-primary truncate font-bold">{name}</p>
              <p className="font-body text-outline truncate text-xs">{roleLabel}</p>
            </Link>
            <button
              onClick={handleLogout}
              aria-label="Sign out"
              title="Sign out"
              className="text-outline-variant hover:text-error transition-colors active:scale-90"
            >
              <LogOut className="size-5" />
            </button>
          </>
        )}
      </div>
    </>
  )
}

export default function Sidebar({
  collapsed,
  onToggleCollapse,
  mobileOpen,
  onMobileClose,
  onOpenCopilot,
}: SidebarProps) {
  return (
    <>
      {/* Persistent desktop rail (non-modal). */}
      <aside
        className={cn(
          'neo-extruded bg-background fixed top-0 bottom-0 left-0 z-40 hidden h-dvh flex-col gap-2 p-4 transition-all duration-300 lg:flex',
          collapsed ? 'w-20' : 'w-64',
        )}
      >
        <SidebarBody
          collapsed={collapsed}
          onToggleCollapse={onToggleCollapse}
          onOpenCopilot={onOpenCopilot}
        />
      </aside>

      {/* Mobile drawer — Radix Sheet: focus trap + Escape + scroll lock (F11). */}
      <Sheet open={mobileOpen} onOpenChange={(next) => !next && onMobileClose()}>
        <SheetContent
          side="left"
          className="neo-extruded bg-background w-64 gap-2 p-4 lg:hidden"
        >
          <SheetTitle className="sr-only">Navigation menu</SheetTitle>
          <SheetDescription className="sr-only">
            Primary navigation and account actions
          </SheetDescription>
          <SidebarBody
            collapsed={false}
            onNavigate={onMobileClose}
            onOpenCopilot={() => {
              onMobileClose()
              onOpenCopilot()
            }}
          />
        </SheetContent>
      </Sheet>
    </>
  )
}
