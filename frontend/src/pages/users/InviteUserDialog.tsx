import { type ReactNode, useState } from 'react'
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
import { ApiError } from '@/api/types'
import { useInviteUser, useRoles } from '@/api/users'

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

/** Invite modal (module spec §12): email, name, phone, one-or-more roles. */
export function InviteUserDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const inviteUser = useInviteUser()
  const { data: rolesData } = useRoles()
  const availableRoles = rolesData?.items ?? []

  const {
    register,
    control,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', first_name: '', last_name: '', phone: '', roles: [] },
  })

  const { fields, append, remove } = useFieldArray({ control, name: 'roles' })

  function close() {
    setOpen(false)
    reset()
  }

  async function onSubmit(values: FormValues) {
    try {
      const invited = await inviteUser.mutateAsync({
        email: values.email,
        first_name: values.first_name,
        last_name: values.last_name,
        phone: values.phone || undefined,
        role_ids: values.roles.map((r) => r.role_id),
      })
      toast.success(`Invite created for ${values.email}`, {
        description: invited.invite_token
          ? 'Deliver the one-time invite token to the user through your secure channel.'
          : undefined,
        duration: 8000,
      })
      close()
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 409
          ? 'A user with that email already exists.'
          : 'Could not create the invite. Please try again.'
      toast.error(message)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Invite user</DialogTitle>
          <DialogDescription>
            The user is created with an invited status and sets a password on first login.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
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
                      label={index === 0 ? 'Assign role' : ''}
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
                  aria-label="Remove role"
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
            <Button type="submit" disabled={inviteUser.isPending}>
              {inviteUser.isPending && <Loader2 className="size-4 animate-spin" />}
              Send invite
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
