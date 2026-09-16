import { useState } from 'react'
import { UserCog, UserPlus } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useUsers, type ManagedUser, type UserStatus } from '@/api/users'
import { usersColumns } from './columns'
import { InviteUserDialog } from './InviteUserDialog'
import { ManageRolesDialog } from './ManageRolesDialog'
import { ConfirmUserActionDialog } from './ConfirmUserActionDialog'

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'active', label: 'Active' },
  { value: 'invited', label: 'Invited' },
  { value: 'suspended', label: 'Suspended' },
]

/** Admin user management (module 02 §12): list, invite, roles, deactivate. */
export default function UsersPage() {
  const [statusFilter, setStatusFilter] = useState('all')
  const [rolesTarget, setRolesTarget] = useState<ManagedUser | null>(null)
  const [statusTarget, setStatusTarget] = useState<ManagedUser | null>(null)

  const { data, isLoading, isError, refetch } = useUsers({
    status: statusFilter === 'all' ? undefined : (statusFilter as UserStatus),
  })
  const users = data?.items ?? []

  const inviteButton = (
    <Button className="rounded-full">
      <UserPlus className="size-4" /> Invite user
    </Button>
  )

  return (
    <div className="w-full">
      <PageHeader
        title="Users & Roles"
        subtitle="Staff access, role assignment and account lifecycle."
        actions={<InviteUserDialog trigger={inviteButton} />}
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load users">
          Something went wrong fetching the staff directory.{' '}
          <button onClick={() => refetch()} className="text-secondary font-bold hover:underline">
            Retry
          </button>
        </Alert>
      ) : (
        <DataTable
          columns={usersColumns({
            onManageRoles: setRolesTarget,
            onDeactivate: setStatusTarget,
            onReactivate: setStatusTarget,
          })}
          data={users}
          isLoading={isLoading}
          searchable
          searchPlaceholder="Search name or email…"
          toolbarRight={
            <Select value={statusFilter} onValueChange={setStatusFilter}>
              <SelectTrigger className="w-44">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {STATUS_FILTERS.map((s) => (
                  <SelectItem key={s.value} value={s.value}>
                    {s.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          }
          emptyState={
            <EmptyState
              icon={UserCog}
              title="No users found"
              description="Invite your first staff member to grant them access."
              action={<InviteUserDialog trigger={inviteButton} />}
            />
          }
        />
      )}

      <ManageRolesDialog user={rolesTarget} onClose={() => setRolesTarget(null)} />
      <ConfirmUserActionDialog user={statusTarget} onClose={() => setStatusTarget(null)} />
    </div>
  )
}
