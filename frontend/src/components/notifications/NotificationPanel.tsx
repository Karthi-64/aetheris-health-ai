import { type ReactNode, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'
import { BellOff, CheckCheck, Loader2, RotateCw, Settings2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { EmptyState } from '@/components/ui/empty-state'
import { Skeleton } from '@/components/ui/skeleton'
import {
  useMarkAllNotificationsRead,
  useMarkNotificationRead,
  useNotifications,
  type Notification,
} from '@/api/notifications'
import { ApiError } from '@/api/types'
import { formatDate, formatRelativeTime, formatTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { isInAppPath, kindPresentation } from './kinds'

/**
 * Why the list could not be shown, in words for the user. The server's text is
 * used only where it is written for them (a 400 explains that the account has
 * no hospital, so no notifications); a 5xx is never echoed.
 */
function loadFailure(err: unknown): { message: string; retry: boolean } {
  if (err instanceof ApiError && err.status === 403) {
    return { message: "You don't have access to notifications.", retry: false }
  }
  if (err instanceof ApiError && err.status === 400) return { message: err.message, retry: false }
  return {
    message: "Notifications couldn't be loaded. Check your connection and try again.",
    retry: true,
  }
}

const rowClass = 'flex w-full items-start gap-3 px-4 py-3 text-left'
const interactiveClass =
  'hover:bg-surface-container-low/70 focus-visible:bg-surface-container-low/70 focus-visible:ring-secondary transition-colors outline-none focus-visible:ring-2 focus-visible:ring-inset'

/**
 * One notification. Opening it marks it read; if it carries an in-app `link`
 * it is a real link to that page, otherwise an unread one is a button whose
 * only job is to mark it read, and a read one is plain content.
 */
function NotificationRow({
  notification,
  onOpen,
  onNavigate,
}: {
  notification: Notification
  onOpen: (notification: Notification) => void
  onNavigate: () => void
}) {
  const { icon: Icon, category } = kindPresentation(notification.kind)
  const unread = !notification.is_read

  const content: ReactNode = (
    <>
      <span
        className={cn(
          'mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl',
          unread ? 'bg-secondary/15 text-secondary' : 'bg-surface-container-high text-outline',
        )}
      >
        <Icon className="size-4" aria-hidden />
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-start justify-between gap-2">
          <span
            className={cn(
              'font-body text-body-sm text-on-surface',
              unread ? 'font-bold' : 'font-medium',
            )}
          >
            {unread && <span className="sr-only">Unread: </span>}
            {notification.title}
          </span>
          {unread && <span aria-hidden className="bg-secondary mt-1.5 size-2 shrink-0 rounded-full" />}
        </span>
        <span className="font-body text-on-surface-variant mt-0.5 block text-xs leading-relaxed break-words">
          {notification.body}
        </span>
        <span className="font-body text-outline mt-1 block text-[11px]">
          {category && <>{category} · </>}
          <time
            dateTime={notification.created_at}
            title={`${formatDate(notification.created_at)}, ${formatTime(notification.created_at)}`}
          >
            {formatRelativeTime(notification.created_at)}
          </time>
        </span>
      </span>
    </>
  )

  const tone = unread && 'bg-secondary/5'

  if (isInAppPath(notification.link)) {
    return (
      <Link
        to={notification.link}
        onClick={() => {
          onOpen(notification)
          onNavigate()
        }}
        className={cn(rowClass, interactiveClass, tone)}
      >
        {content}
      </Link>
    )
  }
  if (unread) {
    return (
      <button
        type="button"
        title="Mark as read"
        onClick={() => onOpen(notification)}
        className={cn(rowClass, interactiveClass, tone)}
      >
        {content}
      </button>
    )
  }
  return <div className={rowClass}>{content}</div>
}

function FilterButton({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'font-label text-label-caps focus-visible:ring-secondary rounded-full px-3 py-1 transition-colors outline-none focus-visible:ring-2',
        active
          ? 'bg-secondary/15 text-secondary'
          : 'text-on-surface-variant hover:bg-surface-container-low',
      )}
    >
      {children}
    </button>
  )
}

/**
 * The notification centre shown under the bell (docs/18-API_CONTRACTS.md §7.2):
 * the caller's notifications newest first, an unread filter, mark-all-read and
 * a way to older pages.
 */
export function NotificationPanel({
  unread,
  onNavigate,
}: {
  /** The bell's unread count, so "mark all" is offered only when there is something to mark. */
  unread: number
  /** Called when a link is followed, so the popover can close. */
  onNavigate: () => void
}) {
  const [unreadOnly, setUnreadOnly] = useState(false)
  const list = useNotifications(unreadOnly)
  const markRead = useMarkNotificationRead()
  const markAll = useMarkAllNotificationsRead()

  const notifications = list.data?.pages.flatMap((page) => page.items) ?? []

  function open(notification: Notification) {
    if (notification.is_read) return
    markRead.mutate(notification.id, {
      onError: () => toast.error("Couldn't mark that notification as read. Please try again."),
    })
  }

  async function markAllRead() {
    try {
      await markAll.mutateAsync()
    } catch {
      toast.error("Couldn't mark notifications as read. Please try again.")
    }
  }

  let body: ReactNode
  if (list.isPending) {
    body = (
      <div role="status" aria-label="Loading notifications" className="space-y-4 p-4">
        {[0, 1, 2].map((i) => (
          <div key={i} className="flex gap-3">
            <Skeleton className="size-9 shrink-0 rounded-xl" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3.5 w-2/3" />
              <Skeleton className="h-3 w-full" />
            </div>
          </div>
        ))}
      </div>
    )
  } else if (list.isError) {
    const failure = loadFailure(list.error)
    body = (
      <div role="alert" className="flex flex-col items-center gap-3 px-6 py-10 text-center">
        <p className="font-body text-body-sm text-on-surface-variant">{failure.message}</p>
        {failure.retry && (
          <Button variant="outline" size="sm" onClick={() => list.refetch()}>
            <RotateCw className="size-4" /> Retry
          </Button>
        )}
      </div>
    )
  } else if (notifications.length === 0) {
    body = (
      <EmptyState
        icon={BellOff}
        className="py-10"
        title={unreadOnly ? "You're all caught up" : 'No notifications yet'}
        description={
          unreadOnly
            ? 'You have no unread notifications.'
            : 'Announcements and anything that needs your attention will appear here.'
        }
      />
    )
  } else {
    body = (
      <>
        <ul aria-label="Notifications" className="divide-outline-variant/20 divide-y">
          {notifications.map((n) => (
            <li key={n.id}>
              <NotificationRow notification={n} onOpen={open} onNavigate={onNavigate} />
            </li>
          ))}
        </ul>
        {list.hasNextPage && (
          <div className="border-outline-variant/20 border-t p-2">
            <Button
              variant="ghost"
              size="sm"
              className="w-full"
              disabled={list.isFetchingNextPage}
              onClick={() => list.fetchNextPage()}
            >
              {list.isFetchingNextPage && <Loader2 className="size-4 animate-spin" />}
              {list.isFetchingNextPage ? 'Loading…' : 'Show older notifications'}
            </Button>
          </div>
        )}
      </>
    )
  }

  return (
    <div className="flex max-h-[min(34rem,calc(100dvh-6rem))] flex-col">
      <div className="border-outline-variant/30 space-y-3 border-b p-4">
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-display text-title-lg text-primary font-bold">Notifications</h2>
          {unread > 0 && (
            <Button
              variant="ghost"
              size="sm"
              disabled={markAll.isPending}
              aria-busy={markAll.isPending}
              onClick={() => markAllRead()}
            >
              {markAll.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <CheckCheck className="size-4" />
              )}
              Mark all as read
            </Button>
          )}
        </div>
        <div role="group" aria-label="Filter notifications" className="flex gap-1.5">
          <FilterButton active={!unreadOnly} onClick={() => setUnreadOnly(false)}>
            All
          </FilterButton>
          <FilterButton active={unreadOnly} onClick={() => setUnreadOnly(true)}>
            Unread{unread > 0 ? ` (${unread})` : ''}
          </FilterButton>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">{body}</div>

      <div className="border-outline-variant/30 border-t p-2">
        <Link
          to="/settings/profile"
          onClick={onNavigate}
          className="text-on-surface-variant hover:text-secondary focus-visible:ring-secondary font-body text-body-sm flex items-center justify-center gap-2 rounded-lg px-3 py-2 transition-colors outline-none focus-visible:ring-2"
        >
          <Settings2 className="size-4" /> Notification preferences
        </Link>
      </div>
    </div>
  )
}
