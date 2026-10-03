import { useState } from 'react'
import { toast } from 'sonner'
import { Loader2, Save } from 'lucide-react'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { ApiError } from '@/api/types'
import {
  useHospitalSettings,
  useUpdateHospitalSettings,
  type UpdateHospitalSettingsInput,
} from '@/api/hospitals'
import { usePermissions } from '@/hooks/usePermissions'

interface FormState {
  name: string
  email: string
  phone: string
  locale: string
  line1: string
  city: string
  state: string
  postal_code: string
  country: string
}

const EMPTY: FormState = {
  name: '',
  email: '',
  phone: '',
  locale: '',
  line1: '',
  city: '',
  state: '',
  postal_code: '',
  country: '',
}

/**
 * Hospital profile tab (module 14 §12, General tab).
 *
 * Editable fields are form inputs; `slug`, `timezone`, `currency` and
 * `tax_id` render read-only because the backend refuses to change them
 * without Superadmin approval (§9) — showing them as inputs would promise a
 * save that cannot happen.
 */
export function HospitalSettingsTab() {
  const { can } = usePermissions()
  const readOnly = !can('settings.update')
  const { data, isLoading, isError, refetch } = useHospitalSettings()
  const update = useUpdateHospitalSettings()
  // Local edits are an override map layered over the loaded record — the
  // form never mirrors query data through an effect, so a background refetch
  // cannot clobber what the admin is typing.
  const [overrides, setOverrides] = useState<Partial<FormState>>({})

  const form: FormState = {
    ...EMPTY,
    ...(data
      ? {
          name: data.name ?? '',
          email: data.email ?? '',
          phone: data.phone ?? '',
          locale: data.locale ?? '',
          line1: data.address.line1 ?? data.address.street ?? '',
          city: data.address.city ?? '',
          state: data.address.state ?? '',
          postal_code: data.address.postal_code ?? data.address.zip ?? '',
          country: data.address.country ?? '',
        }
      : {}),
    ...overrides,
  }

  function set<K extends keyof FormState>(key: K, value: string) {
    setOverrides((prev) => ({ ...prev, [key]: value }))
  }

  async function onSave() {
    if (!data) return

    // Canonical address keys (finding 6): patients already store `line1` /
    // `postal_code`, so the hospital must too. Merge over the existing object
    // so keys this form does not render (e.g. a landmark) survive, and drop the
    // legacy `street`/`zip` pair so a record never carries both key sets.
    const address: Record<string, string> = { ...data.address }
    delete address.street
    delete address.zip
    const addressFields: Record<string, string> = {
      line1: form.line1,
      city: form.city,
      state: form.state,
      postal_code: form.postal_code,
      country: form.country,
    }
    for (const [key, value] of Object.entries(addressFields)) {
      // A filled input writes the canonical key; an emptied one clears the part
      // (finding 5) instead of silently leaving the old value in place.
      if (value.trim()) address[key] = value.trim()
      else delete address[key]
    }

    // Only fields the admin actually touched are sent (finding 5). NOT NULL
    // fields (name, locale) cannot be cleared: an emptied input is sent as-is
    // so the server's 422 is surfaced, rather than a toast that lies.
    const payload: UpdateHospitalSettingsInput = { address }
    if ('name' in overrides) payload.name = form.name.trim()
    if ('locale' in overrides) payload.locale = form.locale.trim()
    // Nullable fields: `null` is the clear signal the server now applies.
    if ('email' in overrides) payload.email = form.email.trim() || null
    if ('phone' in overrides) payload.phone = form.phone.trim() || null

    try {
      await update.mutateAsync(payload)
      toast.success('Hospital settings saved')
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 422
          ? err.message || 'Some values are not valid — check the highlighted fields.'
          : err instanceof ApiError && err.status === 403
            ? 'You do not have permission to update hospital settings.'
            : 'Could not save the settings. Please try again.'
      toast.error(message)
    }
  }

  if (isLoading) {
    return (
      <Card className="gap-4">
        <CardHeader>
          <CardTitle>Hospital profile</CardTitle>
        </CardHeader>
        <CardContent className="text-on-surface-variant">Loading settings…</CardContent>
      </Card>
    )
  }

  if (isError || !data) {
    return (
      <Alert variant="error" title="Couldn't load settings">
        Something went wrong fetching the hospital record.{' '}
        <button onClick={() => refetch()} className="text-secondary font-bold hover:underline">
          Retry
        </button>
      </Alert>
    )
  }

  return (
    <Card className="gap-4">
      <CardHeader>
        <CardTitle>Hospital profile</CardTitle>
        <CardDescription>
          {readOnly
            ? 'You can view the profile. Updating it requires the settings.update permission.'
            : 'Name, contact details and address shown across the app. Slug, timezone and currency are managed by the platform team.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Hospital name" required>
            {(p) => (
              <Input
                {...p}
                value={form.name}
                disabled={readOnly}
                onChange={(e) => set('name', e.target.value)}
              />
            )}
          </Field>
          <Field label="Locale">
            {(p) => (
              <Input
                {...p}
                value={form.locale}
                placeholder="en-IN"
                disabled={readOnly}
                onChange={(e) => set('locale', e.target.value)}
              />
            )}
          </Field>
          <Field label="Contact email">
            {(p) => (
              <Input
                {...p}
                type="email"
                value={form.email}
                disabled={readOnly}
                onChange={(e) => set('email', e.target.value)}
              />
            )}
          </Field>
          <Field label="Contact phone">
            {(p) => (
              <Input
                {...p}
                value={form.phone}
                disabled={readOnly}
                onChange={(e) => set('phone', e.target.value)}
              />
            )}
          </Field>
        </div>

        <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
          <Field label="Address line">
            {(p) => (
              <Input
                {...p}
                value={form.line1}
                disabled={readOnly}
                onChange={(e) => set('line1', e.target.value)}
              />
            )}
          </Field>
          <Field label="City">
            {(p) => (
              <Input
                {...p}
                value={form.city}
                disabled={readOnly}
                onChange={(e) => set('city', e.target.value)}
              />
            )}
          </Field>
          <Field label="State">
            {(p) => (
              <Input
                {...p}
                value={form.state}
                disabled={readOnly}
                onChange={(e) => set('state', e.target.value)}
              />
            )}
          </Field>
          <Field label="Postal code">
            {(p) => (
              <Input
                {...p}
                value={form.postal_code}
                disabled={readOnly}
                onChange={(e) => set('postal_code', e.target.value)}
              />
            )}
          </Field>
          <Field label="Country">
            {(p) => (
              <Input
                {...p}
                value={form.country}
                disabled={readOnly}
                onChange={(e) => set('country', e.target.value)}
              />
            )}
          </Field>
        </div>

        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Field label="Slug" hint="Platform-managed">
            {(p) => <Input {...p} value={data.slug} readOnly disabled />}
          </Field>
          <Field label="Timezone" hint="Platform-managed">
            {(p) => <Input {...p} value={data.timezone} readOnly disabled />}
          </Field>
          <Field label="Currency" hint="Platform-managed">
            {(p) => <Input {...p} value={data.currency} readOnly disabled />}
          </Field>
          <Field label="Tax ID" hint="Platform-managed">
            {(p) => <Input {...p} value={data.tax_id ?? '—'} readOnly disabled />}
          </Field>
        </div>

        {!readOnly && (
          <div className="flex justify-end">
            <Button className="rounded-full" onClick={onSave} disabled={update.isPending}>
              {update.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Save className="size-4" />
              )}
              Save changes
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
