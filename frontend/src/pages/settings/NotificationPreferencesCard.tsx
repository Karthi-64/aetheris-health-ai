import { useState } from 'react'
import { toast } from 'sonner'
import { BellRing, RotateCw } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  useNotificationPreferences,
  useUpdateNotificationPreferences,
  type KindPreference,
  type NotificationChannel,
} from '@/api/notifications'
import { ApiError } from '@/api/types'
import { usePermissions } from '@/hooks/usePermissions'

const CHANNELS: { key: NotificationChannel; label: string }[] = [
  { key: 'in_app', label: 'In-app' },
  { key: 'email', label: 'Email' },
]

/** Kinds grouped by the server's `category`, keeping the order the server sent. */
function byCategory(kinds: KindPreference[]): [string, KindPreference[]][] {
  const groups = new Map<string, KindPreference[]>()
  for (const kind of kinds) {
    groups.set(kind.category, [...(groups.get(kind.category) ?? []), kind])
  }
  return [...groups.entries()]
}

/**
 * The caller's notification preferences (docs/18-API_CONTRACTS.md §7.3).
 *
 * Drawn entirely from `GET /notifications/preferences`: the kinds, their
 * grouping, which channels are on, which are locked and whether a kind has an
 * email form at all. No kind or channel is listed here that the API does not
 * return. Each switch saves on its own, sending only that change, and then
 * shows what the server says is in effect.
 */
export function NotificationPreferencesCard() {
  const { can } = usePermissions()
  const canRead = can('notification.read.own')
  const canUpdate = can('notification.preference.update.own')
  const { data, isPending, isError, refetch } = useNotificationPreferences({ enabled: canRead })
  const update = useUpdateNotificationPreferences()
  /** The switch being saved, as `kind:channel`. */
  const [saving, setSaving] = useState<string | null>(null)

  if (!canRead) return null

  async function toggle(kind: KindPreference, channel: NotificationChannel, on: boolean) {
    setSaving(`${kind.kind}:${channel}`)
    try {
      await update.mutateAsync({ [kind.kind]: { [channel]: on } })
      toast.success('Notification preference saved')
    } catch (err) {
      toast.error(
        err instanceof ApiError && err.status === 403
          ? "You don't have permission to change notification preferences."
          : err instanceof ApiError && err.status === 422
            ? err.message
            : "Couldn't save that preference. Please try again.",
      )
    } finally {
      setSaving(null)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <BellRing className="text-secondary size-5" /> Notifications
        </CardTitle>
        <CardDescription>
          Choose how you hear about each kind of notification. Changes save straight away.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {isError ? (
          <Alert variant="error" title="Couldn't load your notification preferences">
            <div className="flex flex-col items-start gap-3">
              <p>Something went wrong fetching your preferences.</p>
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RotateCw className="size-4" /> Retry
              </Button>
            </div>
          </Alert>
        ) : isPending ? (
          <div role="status" aria-label="Loading notification preferences" className="space-y-4">
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-full" />
            <Skeleton className="h-10 w-2/3" />
          </div>
        ) : (
          <div className="space-y-6">
            {!canUpdate && (
              <Alert variant="info">
                Your role can view these preferences but not change them.
              </Alert>
            )}

            {byCategory(data.kinds).map(([category, kinds]) => (
              <section key={category} aria-label={category}>
                <div className="border-outline-variant/30 mb-2 flex items-center justify-between gap-4 border-b pb-2">
                  <h3 className="font-label text-label-caps text-on-surface-variant">{category}</h3>
                  <div className="flex gap-6" aria-hidden>
                    {CHANNELS.map((c) => (
                      <span
                        key={c.key}
                        className="font-label text-label-caps text-outline w-14 text-center"
                      >
                        {c.label}
                      </span>
                    ))}
                  </div>
                </div>
                <ul className="space-y-1">
                  {kinds.map((kind) => (
                    <li key={kind.kind} className="flex items-center justify-between gap-4 py-2">
                      <div className="min-w-0">
                        <p className="font-body text-body-md text-on-surface">{kind.label}</p>
                        {kind.critical && (
                          <Badge variant="neutral" className="mt-1">
                            Always on
                          </Badge>
                        )}
                      </div>
                      <div className="flex shrink-0 gap-6">
                        {CHANNELS.map((c) => {
                          const locked = kind.locked_channels.includes(c.key)
                          // A kind with no email form has no email switch at all.
                          if (c.key === 'email' && !kind.email_available) {
                            return (
                              <span key={c.key} className="text-outline-variant w-14 text-center" aria-hidden>
                                —
                              </span>
                            )
                          }
                          return (
                            <span key={c.key} className="flex w-14 justify-center">
                              <Switch
                                checked={kind[c.key]}
                                disabled={locked || !canUpdate || saving !== null}
                                aria-busy={saving === `${kind.kind}:${c.key}`}
                                aria-label={`${kind.label}: ${c.label}`}
                                title={locked ? 'Always on for account security' : undefined}
                                onCheckedChange={(on) => toggle(kind, c.key, on)}
                              />
                            </span>
                          )
                        })}
                      </div>
                    </li>
                  ))}
                </ul>
              </section>
            ))}

            <p className="font-body text-outline text-xs">
              Email is sent only if your hospital has email delivery set up; this page can't show
              whether it is. In-app notifications don't depend on it.
            </p>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
