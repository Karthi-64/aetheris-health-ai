import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight, MapPin, Pencil, Plus, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useLocations, type InventoryLocation } from '@/api/inventory'
import { LocationKindBadge } from '@/components/inventory/InventoryBadges'
import { usePermissions } from '@/hooks/usePermissions'
import { InventoryTabs } from './InventoryTabs'
import { CreateLocationDialog, EditLocationDialog } from './LocationDialog'

const ALL = 'all'

/**
 * The answer is the whole list (below), so the table is told it holds the one
 * and only page: left to itself it would cut the rows into pages of its own
 * and draw Previous / Next over a list the server never paged.
 */
const WHOLE_LIST = { page: 1, totalPages: 1, onPageChange: () => undefined }

/**
 * The places that hold stock (docs/18-API_CONTRACTS.md §10.3), ordered by name
 * by the API.
 *
 * `GET /inventory/locations` is not paginated — the answer is every location —
 * and filters by `is_active` and nothing else: it has no search and no sort,
 * so neither is offered. There is no delete either: a location is switched
 * off, which only stops stock being sent to it. Stock held at a location is
 * one click away for those who hold `inventory.stock.read`. Adding and editing
 * are shown only to holders of `inventory.location.create` /
 * `inventory.location.update`.
 */
export default function LocationsPage() {
  const { can } = usePermissions()
  const canCreate = can('inventory.location.create')
  const canEdit = can('inventory.location.update')
  const canReadStock = can('inventory.stock.read')
  const [active, setActive] = useState<string>(ALL)

  const { data, isPending, isError, refetch } = useLocations({
    // Left out for "all": an empty value is a 422, not "no filter".
    is_active: active === ALL ? undefined : active === 'active',
  })

  const locations = data ?? []
  const filtered = active !== ALL

  const columns: ColumnDef<InventoryLocation>[] = useMemo(
    () => [
      {
        accessorKey: 'name',
        header: 'Name',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface block max-w-72 min-w-36 font-semibold [overflow-wrap:anywhere]">
            {row.original.name}
          </span>
        ),
      },
      {
        accessorKey: 'code',
        header: 'Code',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant block max-w-40 font-mono text-xs [overflow-wrap:anywhere]">
            {row.original.code}
          </span>
        ),
      },
      {
        accessorKey: 'kind',
        header: 'Kind',
        enableSorting: false,
        cell: ({ row }) => <LocationKindBadge kind={row.original.kind} />,
      },
      {
        accessorKey: 'is_active',
        header: 'Status',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.is_active ? <Badge variant="success">Active</Badge> : <Badge variant="neutral">Inactive</Badge>,
      },
      {
        id: 'actions',
        header: '',
        enableSorting: false,
        cell: ({ row }) => (
          <div className="flex items-center justify-end gap-3">
            {canEdit && (
              // The table keeps a cell by its row's position. Keyed by the
              // location, an open form is closed if a refetch puts another
              // location in this row, rather than saving its values onto it.
              <EditLocationDialog
                key={row.original.id}
                location={row.original}
                trigger={
                  <Button variant="ghost" size="sm" aria-label={`Edit ${row.original.name}`}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                }
              />
            )}
            {canReadStock && (
              <Link
                to={`/inventory/stock?location_id=${row.original.id}`}
                aria-label={`Stock at ${row.original.name}`}
                className="text-outline hover:text-secondary font-body text-body-sm inline-flex items-center gap-1 whitespace-nowrap transition-colors"
              >
                Stock <ChevronRight className="size-4" />
              </Link>
            )}
          </div>
        ),
      },
    ],
    [canEdit, canReadStock],
  )

  const addButton = (
    <Button className="rounded-full">
      <Plus className="size-4" /> Add location
    </Button>
  )

  return (
    <div className="w-full">
      <InventoryTabs />
      <PageHeader
        title="Locations"
        subtitle="The wards, theatres, ICUs and stores that hold stock."
        actions={
          <>
            <Select value={active} onValueChange={setActive}>
              <SelectTrigger aria-label="Filter by status" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Active and inactive</SelectItem>
                <SelectItem value="active">Active only</SelectItem>
                <SelectItem value="inactive">Inactive only</SelectItem>
              </SelectContent>
            </Select>
            {canCreate && <CreateLocationDialog trigger={addButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load locations">
          <div className="flex flex-col items-start gap-3">
            <p>The location list could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={locations}
          isLoading={isPending}
          serverPagination={WHOLE_LIST}
          emptyState={
            filtered ? (
              <EmptyState
                icon={MapPin}
                title="No matching locations"
                description={
                  active === 'active'
                    ? 'No location is active at the moment.'
                    : 'No location has been switched off.'
                }
              />
            ) : (
              <EmptyState
                icon={MapPin}
                title="No locations yet"
                description={
                  canCreate
                    ? 'Add the wards, theatres and stores that hold stock. Stock is always recorded at a location.'
                    : 'Locations added by someone who manages inventory will appear here.'
                }
                action={canCreate ? <CreateLocationDialog trigger={addButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
