import { useEffect } from 'react'
import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2, ShieldCheck, UserRound } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Alert } from '@/components/ui/alert'
import { Skeleton } from '@/components/ui/skeleton'
import { ApiError } from '@/api/types'
import { useMyProfile, useUpdateMyProfile } from '@/api/profile'
import { useAuthStore } from '@/store/auth-store'
import { ChangePasswordDialog } from './ChangePasswordDialog'
import { MfaCard } from './MfaCard'

/** Optional phone: either blank or E.164, matching `UserProfileUpdateRequest`. */
const phoneOrEmpty = z.union([
  z.literal(''),
  z.string().regex(/^\+?[1-9]\d{1,14}$/, 'Use E.164 format, e.g. +919812345678'),
])

const schema = z.object({
  first_name: z.string().min(1, 'First name is required').max(100),
  last_name: z.string().min(1, 'Last name is required').max(100),
  phone: phoneOrEmpty,
})

type FormValues = z.input<typeof schema>

/**
 * Self-service profile (module spec §12). Every authenticated user reaches
 * this page — it is guarded by `RequireAuth` alone, never by a `user.*`
 * permission, because it only ever acts on the caller's own account.
 */
export default function ProfilePage() {
  const { data: profile, isLoading, isError, refetch } = useMyProfile()
  const updateProfile = useUpdateMyProfile()
  const setUser = useAuthStore((s) => s.setUser)

  const {
    register,
    handleSubmit,
    reset,
    formState: { errors, isDirty, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { first_name: '', last_name: '', phone: '' },
  })

  // The form is populated from the server, so it can only be seeded once the
  // query resolves.
  useEffect(() => {
    if (profile) {
      reset({
        first_name: profile.first_name,
        last_name: profile.last_name,
        phone: profile.phone ?? '',
      })
    }
  }, [profile, reset])

  async function onSubmit(values: FormValues) {
    try {
      const updated = await updateProfile.mutateAsync({
        first_name: values.first_name,
        last_name: values.last_name,
        // The backend's pattern rejects "", so an emptied field is omitted.
        phone: values.phone ? values.phone : undefined,
      })

      // The sidebar and top bar read the display name from the auth store, so
      // a rename has to land there too. Only the name changes — the session's
      // tokens and permissions are left exactly as the server issued them.
      setUser({ name: `${updated.first_name} ${updated.last_name}`.trim() })

      reset({
        first_name: updated.first_name,
        last_name: updated.last_name,
        phone: updated.phone ?? '',
      })
      toast.success('Profile updated')
    } catch (err) {
      toast.error(
        err instanceof ApiError && err.status === 422
          ? 'Check the highlighted fields and try again.'
          : 'Could not save your profile. Please try again.',
      )
    }
  }

  if (isError) {
    return (
      <div className="w-full">
        <PageHeader title="My profile" subtitle="Your account details and security." />
        <Alert variant="error" title="Couldn't load your profile">
          Something went wrong fetching your account.{' '}
          <button onClick={() => refetch()} className="text-secondary font-bold hover:underline">
            Retry
          </button>
        </Alert>
      </div>
    )
  }

  return (
    <div className="w-full max-w-3xl">
      <PageHeader title="My profile" subtitle="Your account details and security." />

      <div className="space-y-6">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <UserRound className="text-secondary size-5" /> Details
            </CardTitle>
            <CardDescription>
              Your name and phone number. Email and roles are managed by an administrator.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="space-y-4">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-2/3" />
              </div>
            ) : (
              <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
                <div className="grid gap-4 sm:grid-cols-2">
                  <Field label="First name" required error={errors.first_name?.message}>
                    {(props) => <Input {...props} {...register('first_name')} />}
                  </Field>
                  <Field label="Last name" required error={errors.last_name?.message}>
                    {(props) => <Input {...props} {...register('last_name')} />}
                  </Field>
                </div>

                <Field
                  label="Phone"
                  error={errors.phone?.message}
                  hint="Optional. International format, e.g. +919812345678."
                >
                  {(props) => <Input {...props} {...register('phone')} />}
                </Field>

                <div className="border-outline-variant/30 grid gap-4 border-t pt-4 sm:grid-cols-2">
                  <div>
                    <p className="font-label text-outline text-xs uppercase">Email</p>
                    <p className="font-body text-body-md text-on-surface mt-1 break-all">
                      {profile?.email}
                    </p>
                  </div>
                  <div>
                    <p className="font-label text-outline text-xs uppercase">Roles</p>
                    <div className="mt-1 flex flex-wrap gap-2">
                      {profile?.roles?.length ? (
                        profile.roles.map((r) => (
                          <Badge key={r.id} variant="accent">
                            {r.name}
                          </Badge>
                        ))
                      ) : (
                        <span className="font-body text-outline text-sm">No roles assigned</span>
                      )}
                    </div>
                  </div>
                </div>

                <div className="flex justify-end">
                  <Button type="submit" className="rounded-full" disabled={!isDirty || isSubmitting}>
                    {isSubmitting && <Loader2 className="size-4 animate-spin" />}
                    Save changes
                  </Button>
                </div>
              </form>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="text-secondary size-5" /> Password
            </CardTitle>
            <CardDescription>
              Changing your password signs out your other sessions.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ChangePasswordDialog
              trigger={
                <Button variant="secondary" className="rounded-full">
                  Change password
                </Button>
              }
            />
          </CardContent>
        </Card>

        <MfaCard enabled={!!profile?.mfa_enabled} loading={isLoading} />
      </div>
    </div>
  )
}
