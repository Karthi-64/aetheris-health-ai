import { useEffect, useState } from 'react'
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

const PAGE_SIZE = 10

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All statuses' },
  { value: 'active', label: 'Active' },
  { value: 'invited', label: 'Invited' },
  { value: 'suspended', label: 'Suspended' },
]

/** Admin user management (module 02 §12): list, invite, roles, deactivate. */
export default function UsersPage() {
  const [statusFilter, setStatusFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [page, setPage] = useState(1)
  const [rolesTarget, setRolesTarget] = useState<ManagedUser | null>(null)
  const [statusTarget, setStatusTarget] = useState<ManagedUser | null>(null)

  // The directory is paged by the server (FR-5 searches across all staff, not
  // just the rows already on screen), so the search term has to reach the API
  // — debounced, to avoid a request per keystroke.
  useEffect(() => {
    const id = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(id)
  }, [search])

  // Changing a filter invalidates the current page number, so both are moved
  // together — resetting it in an effect would render the old page first.
  function changeSearch(value: string) {
    setSearch(value)
    setPage(1)
  }

  function changeStatus(value: string) {
    setStatusFilter(value)
    setPage(1)
  }

  const { data, isLoading, isError, refetch } = useUsers({
    page,
    pageSize: PAGE_SIZE,
    status: statusFilter === 'all' ? undefined : (statusFilter as UserStatus),
    search: debouncedSearch || undefined,
  })
  const users = data?.items ?? []
  const pageCount = data?.pagination.totalPages ?? 1

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
          searchValue={search}
          onSearchChange={changeSearch}
          pageSize={PAGE_SIZE}
          serverPagination={{ page, totalPages: pageCount, onPageChange: setPage }}
          toolbarRight={
            <Select value={statusFilter} onValueChange={changeStatus}>
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
              description={
                debouncedSearch || statusFilter !== 'all'
                  ? 'No staff match these filters.'
                  : 'Invite your first staff member to grant them access.'
              }
              action={
                debouncedSearch || statusFilter !== 'all' ? undefined : (
                  <InviteUserDialog trigger={inviteButton} />
                )
              }
            />
          }
        />
      )}

      <ManageRolesDialog user={rolesTarget} onClose={() => setRolesTarget(null)} />
      <ConfirmUserActionDialog user={statusTarget} onClose={() => setStatusTarget(null)} />
    </div>
  )
}
