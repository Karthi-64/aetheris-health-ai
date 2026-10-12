import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import { http } from '@/api/http'
import type { ListQueryOptions, Paginated } from '@/api/types'

/**
 * Notifications API — typed hooks over the contract in
 * `docs/18-API_CONTRACTS.md` §7 (`backend/app/api/v1/notifications.py`,
 * `backend/app/schemas/notification.py`).
 *
 * Every endpoint is about the caller's own notifications; none takes a user id.
 */

/** One in-app notification (`NotificationResponse`). `title` and `body` are plain text. */
export interface Notification {
  id: string
  /** Stable code, e.g. `billing.discount_approval_requested` (§7.4). */
  kind: string
  title: string
  body: string
  /** An in-app path to open, or null. Never an external URL. */
  link: string | null
  is_read: boolean
  read_at: string | null
  created_at: string
}

export type NotificationChannel = 'in_app' | 'email'

/** One row of the preferences page (`KindPreferenceResponse`). */
export interface KindPreference {
  kind: string
  category: string
  label: string
  /** Account-security kinds are always delivered on their default channels. */
  critical: boolean
  in_app: boolean
  email: boolean
  /** False when this kind has no email form at all. */
  email_available: boolean
  /** Channels the user cannot switch off. */
  locked_channels: string[]
}

export interface NotificationPreferences {
  kinds: KindPreference[]
}

/** Body of `PUT /notifications/preferences`: only the kinds and channels that change. */
export type PreferenceChanges = Record<string, Partial<Record<NotificationChannel, boolean>>>

/** Rows per request for the notification centre. */
const PAGE_SIZE = 20

/**
 * How often the bell re-asks for the unread count. There is no push channel
 * yet, so the contract says to poll, and that 30–60 s is plenty (§7.2).
 */
const UNREAD_POLL_MS = 60_000

export const notificationKeys = {
  /** The centre: the lists and the unread count, which change together. */
  all: ['notifications'] as const,
  lists: ['notifications', 'list'] as const,
  list: (unreadOnly: boolean) => ['notifications', 'list', { unreadOnly }] as const,
  unreadCount: ['notifications', 'unread-count'] as const,
  /** Kept apart from `all`: reading a notification does not change preferences. */
  preferences: ['notification-preferences'] as const,
}

type NotificationPages = InfiniteData<Paginated<Notification>>

/** The number for the bell. Polled, and refreshed when the tab regains focus. */
export function useUnreadNotificationCount(options: ListQueryOptions = {}) {
  return useQuery({
    enabled: options.enabled ?? true,
    queryKey: notificationKeys.unreadCount,
    queryFn: () => http.get<{ unread: number }>('/notifications/unread-count'),
    select: (data) => data.unread,
    staleTime: UNREAD_POLL_MS / 2,
    refetchInterval: UNREAD_POLL_MS,
    refetchOnWindowFocus: true,
  })
}

/** The caller's notifications, newest first, a page at a time. */
export function useNotifications(unreadOnly: boolean, options: ListQueryOptions = {}) {
  return useInfiniteQuery({
    enabled: options.enabled ?? true,
    queryKey: notificationKeys.list(unreadOnly),
    queryFn: ({ pageParam }) =>
      http.getPaginated<Notification>('/notifications', {
        params: { ...(unreadOnly ? { unread_only: true } : {}), page: pageParam, page_size: PAGE_SIZE },
      }),
    initialPageParam: 1,
    getNextPageParam: (last) =>
      last.pagination.page < last.pagination.totalPages ? last.pagination.page + 1 : undefined,
    // Always fresh when the centre opens: a stale list is the wrong thing to
    // show someone who just saw the count change.
    staleTime: 0,
  })
}

/**
 * Mark one notification read. Marking one that is already read is a 200.
 *
 * Applied to the cache straight away — the click usually navigates somewhere
 * else, and the bell should not lag behind it — then rolled back if the server
 * refuses, and reconciled with the server either way.
 */
export function useMarkNotificationRead() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => http.post<Notification>(`/notifications/${id}/read`),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: notificationKeys.all })
      const lists = qc.getQueriesData<NotificationPages>({ queryKey: notificationKeys.lists })
      const count = qc.getQueryData<{ unread: number }>(notificationKeys.unreadCount)

      const wasUnread = lists.some(([, data]) =>
        data?.pages.some((page) => page.items.some((n) => n.id === id && !n.is_read)),
      )
      if (wasUnread) {
        const readAt = new Date().toISOString()
        qc.setQueriesData<NotificationPages>({ queryKey: notificationKeys.lists }, (data) =>
          data && {
            ...data,
            pages: data.pages.map((page) => ({
              ...page,
              items: page.items.map((n) =>
                n.id === id ? { ...n, is_read: true, read_at: readAt } : n,
              ),
            })),
          },
        )
        if (count) {
          qc.setQueryData(notificationKeys.unreadCount, { unread: Math.max(0, count.unread - 1) })
        }
      }
      return { lists, count }
    },
    onError: (_err, _id, snapshot) => {
      for (const [key, data] of snapshot?.lists ?? []) qc.setQueryData(key, data)
      if (snapshot?.count) qc.setQueryData(notificationKeys.unreadCount, snapshot.count)
    },
    onSettled: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  })
}

/** Mark every notification read. Settles once the lists and count have refetched. */
export function useMarkAllNotificationsRead() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => http.post<{ marked: number }>('/notifications/read-all'),
    onSuccess: () => qc.invalidateQueries({ queryKey: notificationKeys.all }),
  })
}

/** Every kind with the channels in effect for the caller (§7.3). */
export function useNotificationPreferences(options: ListQueryOptions = {}) {
  return useQuery({
    enabled: options.enabled ?? true,
    queryKey: notificationKeys.preferences,
    queryFn: () => http.get<NotificationPreferences>('/notifications/preferences'),
  })
}

/**
 * Change preferences. The response is what is **now in effect** — which is not
 * always what was asked for (a critical kind stays on) — so it replaces the
 * cached preferences rather than the request being assumed to have worked.
 */
export function useUpdateNotificationPreferences() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (changes: PreferenceChanges) =>
      http.put<NotificationPreferences>('/notifications/preferences', { preferences: changes }),
    onSuccess: (inEffect) => qc.setQueryData(notificationKeys.preferences, inEffect),
  })
}
