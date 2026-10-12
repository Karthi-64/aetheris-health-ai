import { type ReactNode, useId, useRef, useState } from 'react'
import { Controller, useFieldArray, useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2, Plus, Trash2 } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { useInviteUser, useRoles, type InvitationDelivery, type RoleSummary } from '@/api/users'
import { apiErrorMessage, splitFieldErrors } from '@/lib/apiErrors'
import { MOCK_PERMISSIONS_BY_ROLE, ROLE_KEY_BY_NAME } from '@/lib/rbac'
import { useAuthStore } from '@/store/auth-store'

const phoneOrEmpty = z.union([
  z.literal(''),
  z.string().regex(/^\+?[1-9]\d{1,14}$/, 'Use E.164 format, e.g. +919812345678'),
])

const schema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  first_name: z.string().min(1, 'First name is required').max(100),
  last_name: z.string().min(1, 'Last name is required').max(100),
  phone: phoneOrEmpty.optional(),
  roles: z.array(z.object({ role_id: z.string().min(1, 'Select a role') })).max(5),
})

type FormValues = z.input<typeof schema>

type FormField = 'email' | 'first_name' | 'last_name' | 'phone' | `roles.${number}.role_id`

/** Where a field named in a 422 from `POST /users` is shown on this form. */
function serverField(field: string, roleRows: number): FormField | undefined {
  if (field === 'email' || field === 'first_name' || field === 'last_name' || field === 'phone') {
    return field
  }
  const row = /^role_ids\.(\d+)$/.exec(field)
  if (row && Number(row[1]) < roleRows) return `roles.${Number(row[1])}.role_id`
  return undefined
}

/**
 * Whether the signed-in user may grant `role`. An invite is refused if it
 * carries a role with a permission the actor does not hold
 * (`backend/app/services/user_service.py` `_assert_grantable`). `GET /roles`
 * does not list a role's permissions, so this is known only for the seeded
 * system roles; any other role stays on offer and the API decides.
 */
function canGrant(role: RoleSummary, held: string[]): boolean {
  const key = role.is_system ? ROLE_KEY_BY_NAME[role.name] : undefined
  if (!key) return true
  // `dashboard.view` is a client-only code the API never issues or checks.
  return MOCK_PERMISSIONS_BY_ROLE[key].every((p) => p === 'dashboard.view' || held.includes(p))
}

/** The invite just created: who it is for and whether an email went out. */
interface CreatedInvite {
  email: string
  delivery: InvitationDelivery
}

/**
 * Invite modal (module spec §12): email, name, phone, one-or-more roles.
 *
 * `POST /users` creates the account as Invited. The one-time link that lets
 * the user set a password is delivered by email only and is never returned to,
 * shown by or held in this app. The response says only whether an invitation
 * email was queued; if it was not, the invite stays pending until one is sent
 * with Resend invitation on the Users page.
 */
export function InviteUserDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const [created, setCreated] = useState<CreatedInvite | null>(null)
  // `isPending` only disables the button after a re-render; this also stops a
  // second submit fired before that happens.
  const attempt = useRef(0)
  const submitting = useRef(false)
  const warningId = useId()
  const inviteUser = useInviteUser()
  const { data: rolesData } = useRoles()
  const held = useAuthStore((s) => s.user?.permissions)
  const availableRoles = (rolesData?.items ?? []).filter((r) => canGrant(r, held ?? []))

  const {
    register,
    control,
    handleSubmit,
    reset,
    setError,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', first_name: '', last_name: '', phone: '', roles: [] },
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'roles' })

  function close() {
    // An invite still in flight must not write its result into a dialog that
    // has been closed (or reopened for someone else).
    attempt.current += 1
    setOpen(false)
    setNotice(null)
    setCreated(null)
    inviteUser.reset()
    reset()
  }

  async function onSubmit(values: FormValues) {
    if (submitting.current) return
    submitting.current = true
    const mine = attempt.current
    setNotice(null)
    try {
      const invited = await inviteUser.mutateAsync({
        email: values.email,
        first_name: values.first_name,
        last_name: values.last_name,
        phone: values.phone || undefined,
        role_ids: values.roles.map((r) => r.role_id),
      })
      if (invited.invitation.delivery === 'queued') {
        toast.success(`Invite created for ${values.email}`)
      }
      if (mine !== attempt.current) {
        // The dialog was closed meanwhile. "No email was sent" must still be
        // said somewhere.
        if (invited.invitation.delivery !== 'queued') {
          toast.error(`${values.email} was added, but no invitation email was sent.`, {
            description: 'Use Resend invitation on the Users page once email delivery is working.',
          })
        }
        return
      }
      setCreated({ email: values.email, delivery: invited.invitation.delivery })
    } catch (err) {
      // A 422 names the fields it rejects: each goes under its input, and one
      // this form does not show goes above the form rather than being lost.
      const { onFields, other } = splitFieldErrors(
        err,
        (field) => serverField(field, values.roles.length) !== undefined,
      )
      onFields.forEach((fe, index) => {
        const target = serverField(fe.field, values.roles.length)
        if (target) setError(target, { message: fe.message }, { shouldFocus: index === 0 })
      })
      // Any other refusal in the API's own words: the email is taken (409), a
      // role grants more than the admin holds (403), a role is gone (404).
      const message =
        other.length > 0
          ? other.join(' ')
          : onFields.length === 0
            ? apiErrorMessage(err, "Couldn't create the invite. Please try again.")
            : null
      setNotice(message)
      toast.error(message ?? "Couldn't create the invite. Check the highlighted fields.")
    } finally {
      submitting.current = false
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent
        className="max-w-xl"
        // The warning is this view's description; the other views bring their own.
        {...(created?.delivery === 'unavailable' ? { 'aria-describedby': warningId } : {})}
      >
        {created ? (
          <>
            {created.delivery === 'queued' ? (
              <DialogHeader>
                <DialogTitle>Invite created</DialogTitle>
                <DialogDescription>
                  {created.email} has been added with Invited status. An invitation email has been
                  queued for this address. Delivery is not confirmed. If it does not arrive, use
                  Resend invitation on the Users page.
                </DialogDescription>
              </DialogHeader>
            ) : (
              <>
                <DialogHeader>
                  <DialogTitle>Invite created, email not sent</DialogTitle>
                </DialogHeader>
                <Alert variant="warning" id={warningId}>
                  {created.email} has been added with Invited status, but no invitation email was
                  sent because email delivery is not available. The user cannot set a password
                  until an invitation is sent. Use Resend invitation on the Users page once email
                  delivery is working.
                </Alert>
              </>
            )}

            <DialogFooter>
              <Button type="button" onClick={close}>
                Done
              </Button>
            </DialogFooter>
          </>
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Invite user</DialogTitle>
              <DialogDescription>
                Creates the account with Invited status. The user sets a password from a one-time
                link sent to their email address. The link is not shown here.
              </DialogDescription>
            </DialogHeader>

            <form onSubmit={(e) => handleSubmit(onSubmit)(e)} className="space-y-4" noValidate>
              {notice && (
                <Alert variant="error" title="Couldn't create the invite">
                  {notice}
                </Alert>
              )}

              <Field label="Email" required error={errors.email?.message}>
                {(p) => (
                  <Input
                    type="email"
                    placeholder="clinician@hospital.org"
                    autoComplete="off"
                    {...p}
                    {...register('email')}
                  />
                )}
              </Field>

              <div className="grid grid-cols-2 gap-4">
                <Field label="First name" required error={errors.first_name?.message}>
                  {(p) => <Input placeholder="Priya" {...p} {...register('first_name')} />}
                </Field>
                <Field label="Last name" required error={errors.last_name?.message}>
                  {(p) => <Input placeholder="Nair" {...p} {...register('last_name')} />}
                </Field>
              </div>

              <Field
                label="Phone"
                error={errors.phone?.message}
                hint="Optional. International format, e.g. +919812345678."
              >
                {(p) => <Input type="tel" placeholder="+919812345678" {...p} {...register('phone')} />}
              </Field>

              <div className="space-y-2">
                <p className="font-label text-label-caps text-on-surface-variant">Roles</p>
                {fields.map((field, index) => (
                  <div key={field.id} className="flex items-start gap-2">
                    <Controller
                      control={control}
                      name={`roles.${index}.role_id`}
                      render={({ field: f }) => (
                        <Field
                          label={index === 0 ? 'Assign role' : `Role ${index + 1}`}
                          required={index === 0}
                          error={errors.roles?.[index]?.role_id?.message}
                          className="flex-1"
                        >
                          {(p) => (
                            <Select value={f.value} onValueChange={f.onChange}>
                              <SelectTrigger id={p.id} aria-invalid={p['aria-invalid']}>
                                <SelectValue placeholder="Select a role" />
                              </SelectTrigger>
                              <SelectContent>
                                {availableRoles.map((r) => (
                                  <SelectItem key={r.id} value={r.id}>
                                    {r.name}
                                  </SelectItem>
                                ))}
                              </SelectContent>
                            </Select>
                          )}
                        </Field>
                      )}
                    />
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="mt-1 text-outline-variant hover:text-error"
                      aria-label={`Remove role ${index + 1}`}
                      onClick={() => remove(index)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </div>
                ))}
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => append({ role_id: '' })}
                  disabled={availableRoles.length === 0}
                >
                  <Plus className="size-4" /> Add role
                </Button>
              </div>

              <DialogFooter>
                <Button type="button" variant="ghost" onClick={close}>
                  Cancel
                </Button>
                <Button type="submit" disabled={inviteUser.isPending} aria-busy={inviteUser.isPending}>
                  {inviteUser.isPending && <Loader2 className="size-4 animate-spin" />}
                  Create invite
                </Button>
              </DialogFooter>
            </form>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
