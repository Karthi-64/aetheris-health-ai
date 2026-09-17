import { type ReactNode, useState } from 'react'
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
  DialogTrigger,
} from '@/components/ui/dialog'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/api/types'
import { useChangePassword } from '@/api/profile'

// 12 characters is the backend's floor (`ChangePasswordRequest.new_password`);
// rejecting a short password here saves a round trip, but the server is still
// the one that decides.
const schema = z
  .object({
    current_password: z.string().min(1, 'Enter your current password'),
    new_password: z.string().min(12, 'Use at least 12 characters').max(128),
    confirm_password: z.string().min(1, 'Repeat the new password'),
  })
  .refine((v) => v.new_password === v.confirm_password, {
    message: 'The two passwords do not match',
    path: ['confirm_password'],
  })
  .refine((v) => v.new_password !== v.current_password, {
    message: 'Choose a password you have not used here before',
    path: ['new_password'],
  })

type FormValues = z.input<typeof schema>

/** Change-password dialog for the signed-in user (module spec §12). */
export function ChangePasswordDialog({ trigger }: { trigger: ReactNode }) {
  const [open, setOpen] = useState(false)
  const changePassword = useChangePassword()

  const {
    register,
    handleSubmit,
    reset,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: '', new_password: '', confirm_password: '' },
  })

  function close() {
    setOpen(false)
    reset()
  }

  async function onSubmit(values: FormValues) {
    try {
      await changePassword.mutateAsync({
        current_password: values.current_password,
        new_password: values.new_password,
      })
      toast.success('Password changed', {
        description: 'Your other sessions have been signed out.',
      })
      close()
    } catch (err) {
      // 401 is specifically "current password is wrong" on this endpoint, so it
      // belongs on that field rather than in a toast.
      if (err instanceof ApiError && err.status === 401) {
        setError('current_password', { message: 'That is not your current password' })
        return
      }
      if (err instanceof ApiError && err.status === 400) {
        setError('new_password', {
          message: err.message || 'That password does not meet the requirements',
        })
        return
      }
      toast.error('Could not change your password. Please try again.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? setOpen(true) : close())}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Change password</DialogTitle>
          <DialogDescription>
            You will stay signed in here; other devices will need to sign in again.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
          <Field label="Current password" required error={errors.current_password?.message}>
            {(props) => (
              <Input
                {...props}
                type="password"
                autoComplete="current-password"
                {...register('current_password')}
              />
            )}
          </Field>

          <Field
            label="New password"
            required
            error={errors.new_password?.message}
            hint="At least 12 characters."
          >
            {(props) => (
              <Input
                {...props}
                type="password"
                autoComplete="new-password"
                {...register('new_password')}
              />
            )}
          </Field>

          <Field label="Confirm new password" required error={errors.confirm_password?.message}>
            {(props) => (
              <Input
                {...props}
                type="password"
                autoComplete="new-password"
                {...register('confirm_password')}
              />
            )}
          </Field>

          <DialogFooter>
            <Button type="button" variant="ghost" onClick={close}>
              Cancel
            </Button>
            <Button type="submit" className="rounded-full" disabled={isSubmitting}>
              {isSubmitting && <Loader2 className="size-4 animate-spin" />}
              Change password
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
