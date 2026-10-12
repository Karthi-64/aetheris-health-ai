import { useMemo, useState } from 'react'
import type { ColumnDef } from '@tanstack/react-table'
import { Pencil, Plus, RotateCw, Truck } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useVendors, type Vendor } from '@/api/pharmacy'
import { usePermissions } from '@/hooks/usePermissions'
import { PharmacyTabs } from './PharmacyTabs'
import { CreateVendorDialog, EditVendorDialog } from './VendorDialog'

const PAGE_SIZE = 25
const ALL = 'all'

/** An optional value, or a dash. Long unbroken text (an email, a tax id) wraps inside its cell. */
function Optional({ value }: { value: string | null }) {
  return value ? (
    <span className="text-on-surface-variant block max-w-64 min-w-28 [overflow-wrap:anywhere]">{value}</span>
  ) : (
    <span className="text-outline-variant">—</span>
  )
}

/**
 * The vendors medicines are ordered from (docs/18-API_CONTRACTS.md §9.7),
 * ordered by name by the API.
 *
 * The API filters by `is_active` and nothing else — it has no search and no
 * sort, so neither is offered. There is no delete either: a vendor is switched
 * off, which only stops new orders. Adding and editing are shown only to
 * holders of `pharmacy.vendor.create` / `pharmacy.vendor.update`.
 */
export default function VendorsPage() {
  const { can } = usePermissions()
  const canCreate = can('pharmacy.vendor.create')
  const canEdit = can('pharmacy.vendor.update')
  const [active, setActive] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  const { data, isPending, isError, refetch } = useVendors({
    // Left out for "all": an empty value is a 422, not "no filter".
    is_active: active === ALL ? undefined : active === 'active',
    page,
    page_size: PAGE_SIZE,
  })

  const vendors = data?.items ?? []
  const meta = data?.pagination
  const filtered = active !== ALL

  const columns: ColumnDef<Vendor>[] = useMemo(
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
        accessorKey: 'contact',
        header: 'Contact',
        enableSorting: false,
        cell: ({ row }) => <Optional value={row.original.contact} />,
      },
      {
        accessorKey: 'address',
        header: 'Address',
        enableSorting: false,
        cell: ({ row }) => <Optional value={row.original.address} />,
      },
      {
        accessorKey: 'tax_id',
        header: 'Tax ID',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.tax_id ? (
            <span className="text-on-surface-variant block max-w-48 font-mono text-xs [overflow-wrap:anywhere]">
              {row.original.tax_id}
            </span>
          ) : (
            <span className="text-outline-variant">—</span>
          ),
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
        cell: ({ row }) =>
          canEdit ? (
            <div className="flex justify-end">
              <EditVendorDialog
                vendor={row.original}
                trigger={
                  <Button variant="ghost" size="sm" aria-label={`Edit ${row.original.name}`}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                }
              />
            </div>
          ) : null,
      },
    ],
    [canEdit],
  )

  const addButton = (
    <Button className="rounded-full">
      <Plus className="size-4" /> Add vendor
    </Button>
  )

  return (
    <div className="w-full">
      <PharmacyTabs />
      <PageHeader
        title="Vendors"
        subtitle="The suppliers medicines are ordered from."
        actions={
          <>
            <Select
              value={active}
              onValueChange={(v) => {
                setActive(v)
                setPage(1)
              }}
            >
              <SelectTrigger aria-label="Filter by status" className="w-48 rounded-full py-2.5">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>Active and inactive</SelectItem>
                <SelectItem value="active">Active only</SelectItem>
                <SelectItem value="inactive">Inactive only</SelectItem>
              </SelectContent>
            </Select>
            {canCreate && <CreateVendorDialog trigger={addButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load vendors">
          <div className="flex flex-col items-start gap-3">
            <p>The vendor list could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={vendors}
          isLoading={isPending}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState
                icon={Truck}
                title="No matching vendors"
                description={
                  active === 'active' ? 'No vendor is active at the moment.' : 'No vendor has been switched off.'
                }
              />
            ) : (
              <EmptyState
                icon={Truck}
                title="No vendors yet"
                description={
                  canCreate
                    ? 'Add the suppliers your pharmacy buys from. A purchase order needs an active vendor.'
                    : 'Vendors added by someone who manages purchasing will appear here.'
                }
                action={canCreate ? <CreateVendorDialog trigger={addButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
