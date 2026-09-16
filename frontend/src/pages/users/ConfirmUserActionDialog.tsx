import { useState } from 'react'
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
import { Button } from '@/components/ui/button'
import { ApiError } from '@/api/types'
import { useDeactivateUser, useReactivateUser, type ManagedUser } from '@/api/users'
import { userFullName } from './usersShared'

interface ConfirmUserActionDialogProps {
  /** The user to act on, or null to keep the dialog closed. */
  user: ManagedUser | null
  onClose: () => void
}

/**
 * Confirm-and-run dialog for deactivate/reactivate (cross-cutting standard:
 * confirm destructive actions). Deactivation also revokes all the user's
 * sessions server-side (module spec §5.2).
 */
export function ConfirmUserActionDialog({ user, onClose }: ConfirmUserActionDialogProps) {
  const [pending, setPending] = useState(false)
  const deactivateUser = useDeactivateUser()
  const reactivateUser = useReactivateUser()

  if (!user) return null

  // Only a suspended account can be reactivated; invited and active accounts
  // can be deactivated (module spec §5.2).
  const isDeactivate = user.status !== 'suspended'
  const actionLabel = isDeactivate ? 'Deactivate' : 'Reactivate'

  async function handleConfirm() {
    if (!user) return
    setPending(true)
    try {
      if (isDeactivate) {
        await deactivateUser.mutateAsync(user.id)
        toast.success(`${userFullName(user)} deactivated`, {
          description: 'All their sessions were revoked.',
        })
      } else {
        await reactivateUser.mutateAsync(user.id)
        toast.success(`${userFullName(user)} reactivated`)
      }
      onClose()
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 400
          ? 'You cannot deactivate your own account.'
          : `Could not ${actionLabel.toLowerCase()} the user. Please try again.`
      toast.error(message)
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>
            {actionLabel} {userFullName(user)}?
          </DialogTitle>
          <DialogDescription>
            {isDeactivate
              ? 'The account will be suspended and every active session terminated immediately. History is preserved and the account can be reactivated.'
              : 'The account will regain access with its existing roles and permissions.'}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={pending}>
            Cancel
          </Button>
          <Button
            variant={isDeactivate ? 'destructive' : 'default'}
            onClick={handleConfirm}
            disabled={pending}
          >
            {pending && <Loader2 className="size-4 animate-spin" />}
            {actionLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
