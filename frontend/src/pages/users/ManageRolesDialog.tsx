import { useState } from 'react'
import { toast } from 'sonner'
import { Loader2, Plus, X } from 'lucide-react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ApiError } from '@/api/types'
import {
  useAssignRole,
  useRemoveRole,
  useRoles,
  useUser,
  type ManagedUser,
} from '@/api/users'
import { userFullName } from './usersShared'

interface ManageRolesDialogProps {
  user: ManagedUser | null
  onClose: () => void
}

/** Role assignment surface (module spec §5.3): add/remove roles per user. */
export function ManageRolesDialog({ user: staleUser, onClose }: ManageRolesDialogProps) {
  const [selectedRole, setSelectedRole] = useState('')
  const { data: rolesData, isLoading: rolesLoading } = useRoles()
  const assignRole = useAssignRole()
  const removeRole = useRemoveRole()

  // The `user` prop is a snapshot from the table row; after an assign/remove
  // the query cache holds fresher data (mutations invalidate `users.all`).
  // Re-read by id so the badge list updates without closing the dialog.
  const { data: freshUser } = useUser(staleUser?.id ?? null)
  const user = freshUser ?? staleUser

  const availableRoles = (rolesData?.items ?? []).filter(
    (r) => !user?.roles.some((assigned) => assigned.id === r.id),
  )

  async function handleAssign() {
    if (!user || !selectedRole) return
    try {
      await assignRole.mutateAsync({ userId: user.id, roleId: selectedRole })
      const roleName = availableRoles.find((r) => r.id === selectedRole)?.name
      toast.success(`Assigned ${roleName ?? 'role'} to ${userFullName(user)}`, {
        description: 'Their sessions were revoked so the new permissions take effect.',
      })
      setSelectedRole('')
    } catch {
      toast.error('Could not assign the role. You may lack the role.assign permission.')
    }
  }

  async function handleRemove(roleId: string, roleName: string) {
    if (!user) return
    try {
      await removeRole.mutateAsync({ userId: user.id, roleId })
      toast.success(`Removed ${roleName} from ${userFullName(user)}`)
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 400
          ? 'This role cannot be removed — the user must keep at least one admin role.'
          : 'Could not remove the role. Please try again.'
      toast.error(message)
    }
  }

  return (
    <Dialog open={!!user} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Manage roles</DialogTitle>
          <DialogDescription>
            {user ? `${userFullName(user)} · ${user.email}` : ''}
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <div className="space-y-2">
            <p className="font-label text-label-caps text-on-surface-variant">Assigned roles</p>
            {user && user.roles.length === 0 && (
              <p className="font-body text-body-sm text-outline">
                No roles — this user can sign in but sees nothing.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {user?.roles.map((role) => (
                <span
                  key={role.id}
                  className="neo-pressed bg-surface flex items-center gap-1.5 rounded-full py-1 pr-1.5 pl-3"
                >
                  <span className="font-body text-body-sm">{role.name}</span>
                  {role.is_system && (
                    <Badge variant="neutral" className="px-1.5 py-0 text-[10px]">
                      system
                    </Badge>
                  )}
                  <button
                    type="button"
                    aria-label={`Remove ${role.name}`}
                    disabled={removeRole.isPending}
                    onClick={() => handleRemove(role.id, role.name)}
                    className="text-outline-variant hover:text-error rounded-full p-0.5 transition-colors disabled:opacity-50"
                  >
                    {removeRole.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <X className="size-3.5" />}
                  </button>
                </span>
              ))}
            </div>
          </div>

          <div className="space-y-2">
            <p className="font-label text-label-caps text-on-surface-variant">Assign a role</p>
            <div className="flex gap-2">
              <Select value={selectedRole} onValueChange={setSelectedRole}>
                <SelectTrigger className="flex-1">
                  <SelectValue placeholder={rolesLoading ? 'Loading roles…' : 'Select a role'} />
                </SelectTrigger>
                <SelectContent>
                  {availableRoles.map((r) => (
                    <SelectItem key={r.id} value={r.id}>
                      {r.name}
                      {r.is_system ? ' (system)' : ''}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button onClick={handleAssign} disabled={!selectedRole || assignRole.isPending}>
                {assignRole.isPending ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
                Assign
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
