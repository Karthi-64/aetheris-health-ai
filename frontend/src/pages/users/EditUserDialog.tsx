import { useForm } from 'react-hook-form'
import { zodResolver } from '@hookform/resolvers/zod'
import { z } from 'zod'
import { toast } from 'sonner'
import { Loader2 } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/api/types'
import { useUpdateUser, type ManagedUser } from '@/api/users'

const phoneOrEmpty = z.union([
  z.literal(''),
  z.string().regex(/^\+?[1-9]\d{1,14}$/, 'Use E.164 format, e.g. +919812345678'),
])

const schema = z.object({
  first_name: z.string().min(1, 'First name is required').max(100),
  last_name: z.string().min(1, 'Last name is required').max(100),
  phone: phoneOrEmpty.optional(),
})

type FormValues = z.input<typeof schema>

/**
 * Edit modal for a staff member's profile fields (module 02, feature "edit
 * user"). Email is immutable here — it is the login identity and the backend
 * rejects changing it; roles live in {@link ManageRolesDialog}.
 *
 * The form body is keyed on the user id and mounted only while the dialog is
 * open, so defaults always initialise from the current row — no effect that
 * resets the form after render.
 */
export function EditUserDialog({
  user,
  onClose,
}: {
  user: ManagedUser | null
  onClose: () => void
}) {
  return (
    <Dialog open={!!user} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent className="max-w-xl">
        {user && <EditUserForm key={user.id} user={user} onClose={onClose} />}
      </DialogContent>
    </Dialog>
  )
}

function EditUserForm({ user, onClose }: { user: ManagedUser; onClose: () => void }) {
  const updateUser = useUpdateUser()

  const {
    register,
    handleSubmit,
    formState: { errors },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      first_name: user.first_name,
      last_name: user.last_name,
      phone: user.phone ?? '',
    },
  })

  async function onSubmit(values: FormValues) {
    try {
      await updateUser.mutateAsync({
        id: user.id,
        first_name: values.first_name,
        last_name: values.last_name,
        // An emptied phone must clear the value (PR #29 review finding 9):
        // `undefined` drops the key from the PATCH and the old number survives,
        // while the toast still claims success. The update schema accepts null.
        phone: values.phone || null,
      })
      toast.success('User updated')
      onClose()
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 403
          ? 'You do not have permission to edit users.'
          : 'Could not update the user. Please try again.'
      toast.error(message)
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Edit user</DialogTitle>
        <DialogDescription>
          Update the profile details for {user.first_name} {user.last_name}. The email address
          is the login identity and cannot be changed here.
        </DialogDescription>
      </DialogHeader>

      <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <div className="grid grid-cols-2 gap-4">
          <Field label="First name" required error={errors.first_name?.message}>
            {(p) => <Input placeholder="Priya" {...p} {...register('first_name')} />}
          </Field>
          <Field label="Last name" required error={errors.last_name?.message}>
            {(p) => <Input placeholder="Nair" {...p} {...register('last_name')} />}
          </Field>
        </div>

        <Field label="Phone" error={errors.phone?.message} hint="E.164, e.g. +919812345678">
          {(p) => <Input placeholder="+919812345678" {...p} {...register('phone')} />}
        </Field>

        <Field label="Email" hint="Read-only — used to sign in.">
          {(p) => <Input value={user.email} readOnly disabled {...p} />}
        </Field>

        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" className="rounded-full" disabled={updateUser.isPending}>
            {updateUser.isPending && <Loader2 className="size-4 animate-spin" />}
            Save changes
          </Button>
        </DialogFooter>
      </form>
    </>
  )
}
