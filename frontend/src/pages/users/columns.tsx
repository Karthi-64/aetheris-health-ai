import type { ColumnDef } from '@tanstack/react-table'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import type { ManagedUser } from '@/api/users'
import { USER_STATUS_LABELS, USER_STATUS_VARIANT } from './usersShared'

export interface UsersTableActions {
  onManageRoles: (user: ManagedUser) => void
  onDeactivate: (user: ManagedUser) => void
  onReactivate: (user: ManagedUser) => void
}

function fullName(u: ManagedUser) {
  return `${u.first_name} ${u.last_name}`.trim()
}

function formatDate(iso: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

export function usersColumns(actions: UsersTableActions): ColumnDef<ManagedUser>[] {
  return [
    {
      accessorKey: 'first_name',
      header: 'Name',
      cell: ({ row }) => (
        <div className="flex flex-col">
          <span className="font-semibold">{fullName(row.original)}</span>
          <span className="text-on-surface-variant text-xs">{row.original.email}</span>
        </div>
      ),
    },
    {
      id: 'roles',
      header: 'Roles',
      enableSorting: false,
      cell: ({ row }) => (
        <div className="flex max-w-56 flex-wrap gap-1">
          {row.original.roles.length === 0 ? (
            <span className="text-outline text-xs">No roles</span>
          ) : (
            row.original.roles.map((r) => (
              <Badge key={r.id} variant="primary">
                {r.name}
              </Badge>
            ))
          )}
        </div>
      ),
    },
    {
      accessorKey: 'status',
      header: 'Status',
      cell: ({ row }) => {
        const status = row.original.status
        return (
          <Badge variant={USER_STATUS_VARIANT[status] ?? 'neutral'}>
            {USER_STATUS_LABELS[status] ?? status}
          </Badge>
        )
      },
    },
    {
      accessorKey: 'last_login_at',
      header: 'Last login',
      cell: ({ row }) => <span className="text-on-surface-variant">{formatDate(row.original.last_login_at)}</span>,
    },
    {
      id: 'actions',
      header: '',
      enableSorting: false,
      cell: ({ row }) => {
        const user = row.original
        const canReactivate = user.status === 'suspended'
        return (
          <div className="flex justify-end gap-2">
            <Button variant="ghost" size="sm" onClick={() => actions.onManageRoles(user)}>
              Roles
            </Button>
            {canReactivate ? (
              <Button variant="ghost" size="sm" onClick={() => actions.onReactivate(user)}>
                Reactivate
              </Button>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                className="text-error hover:text-error"
                onClick={() => actions.onDeactivate(user)}
              >
                Deactivate
              </Button>
            )}
          </div>
        )
      },
    },
  ]
}
