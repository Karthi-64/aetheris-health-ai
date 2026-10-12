import { useState } from 'react'
import { Bell } from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useUnreadNotificationCount } from '@/api/notifications'
import { usePermissions } from '@/hooks/usePermissions'
import { cn } from '@/lib/utils'
import { NotificationPanel } from './NotificationPanel'

/** The badge stops counting here; the panel shows the rest. */
const BADGE_MAX = 99

/**
 * The top-bar bell: the unread count, and the notification centre it opens
 * (module spec 11 §12).
 *
 * Renders nothing for a user without `notification.read.own` — every seeded
 * role holds it, and without it each of these endpoints is a 403.
 */
export function NotificationBell() {
  const { can } = usePermissions()
  const allowed = can('notification.read.own')
  const [open, setOpen] = useState(false)
  const { data: unread = 0, isPending } = useUnreadNotificationCount({ enabled: allowed })

  if (!allowed) return null

  const label = unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={label}
          aria-busy={isPending}
          title="Notifications"
          className={cn(
            'hover:text-secondary relative flex size-10 items-center justify-center rounded-lg transition-colors hover:bg-white/20',
            'focus-visible:ring-secondary outline-none focus-visible:ring-2',
            unread > 0 ? 'text-secondary' : 'text-outline-variant',
            isPending && 'animate-pulse',
          )}
        >
          <Bell className="size-5" />
          {unread > 0 && (
            <span
              aria-hidden
              className="bg-error absolute top-1 right-1 flex h-4 min-w-4 items-center justify-center rounded-full px-1 text-[10px] leading-none font-bold text-white"
            >
              {unread > BADGE_MAX ? `${BADGE_MAX}+` : unread}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        aria-label="Notifications"
        className="w-[min(24rem,calc(100vw-1.5rem))] p-0"
      >
        <NotificationPanel unread={unread} onNavigate={() => setOpen(false)} />
      </PopoverContent>
    </Popover>
  )
}
