import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ArrowLeftRight, Boxes, PackageMinus, RotateCw, SlidersHorizontal } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Field } from '@/components/ui/field'
import { useItem, useLocations, useStock, type InventoryItemRef, type StockRow } from '@/api/inventory'
import { ExpiredBadge } from '@/components/inventory/InventoryBadges'
import { isZeroQuantity, plainDateLabel, quantityLabel } from '@/components/inventory/inventoryPresentation'
import { usePermissions } from '@/hooks/usePermissions'
import { InventoryTabs } from './InventoryTabs'
import { AdjustDialog, ConsumeDialog, LocationSelect, TransferDialog } from './StockActions'
import { ItemFilter } from './StockPageFilters'
import { idParam, itemRefOf } from './stockActionForms'

const PAGE_SIZE = 25

/** A stock row in words, to tell one row's buttons from the next row's. */
const rowName = (row: StockRow) =>
  `${row.item_name} at ${row.location_name}${row.batch_number ? `, batch ${row.batch_number}` : ''}`

/**
 * What can be done to one stock row, each under the permission its endpoint
 * requires (docs/18-API_CONTRACTS.md §10.1): `inventory.consume`,
 * `inventory.transfer`, `inventory.adjust`.
 *
 * Consume and transfer draw only on stock that is neither empty nor expired
 * (`lock_usable_stock`, inventory_repository.py), so from an expired or an
 * empty row they could only ever be refused; such a row offers neither. It can
 * still be adjusted — that is how expired stock is written off.
 */
function RowActions({ row }: { row: StockRow }) {
  const { can } = usePermissions()
  const usable = !row.is_expired && !isZeroQuantity(row.quantity)

  return (
    <div className="flex items-center justify-end gap-1">
      {can('inventory.consume') && usable && (
        <ConsumeDialog
          row={row}
          trigger={
            <Button variant="ghost" size="sm" aria-label={`Consume ${rowName(row)}`}>
              <PackageMinus className="size-4" /> Consume
            </Button>
          }
        />
      )}
      {can('inventory.transfer') && usable && (
        <TransferDialog
          row={row}
          trigger={
            <Button variant="ghost" size="sm" aria-label={`Transfer ${rowName(row)}`}>
              <ArrowLeftRight className="size-4" /> Transfer
            </Button>
          }
        />
      )}
      {can('inventory.adjust') && (
        <AdjustDialog
          row={row}
          trigger={
            <Button variant="ghost" size="sm" aria-label={`Adjust ${rowName(row)}`}>
              <SlidersHorizontal className="size-4" /> Adjust
            </Button>
          }
        />
      )}
    </div>
  )
}

/**
 * Stock columns. Sorting is off: the API orders rows by item, location and
 * then expiry, and that order is kept. Whether a batch has expired is the
 * server's `is_expired`, judged on the hospital's own date — never worked out
 * from the date here.
 */
const stockColumns: ColumnDef<StockRow>[] = [
  {
    accessorKey: 'item_name',
    header: 'Item',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex max-w-72 min-w-40 flex-col [overflow-wrap:anywhere]">
        <span className="text-on-surface font-semibold">{row.original.item_name}</span>
        <span className="text-outline font-mono text-xs">{row.original.item_sku}</span>
      </div>
    ),
  },
  {
    accessorKey: 'location_name',
    header: 'Location',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant block max-w-48 min-w-24 [overflow-wrap:anywhere]">
        {row.original.location_name}
      </span>
    ),
  },
  {
    accessorKey: 'batch_number',
    header: 'Batch',
    enableSorting: false,
    cell: ({ row }) =>
      row.original.batch_number ? (
        <span className="text-on-surface block max-w-40 font-mono text-xs [overflow-wrap:anywhere]">
          {row.original.batch_number}
        </span>
      ) : (
        <span className="text-outline">—</span>
      ),
  },
  {
    accessorKey: 'expiry_date',
    header: 'Expiry',
    enableSorting: false,
    cell: ({ row }) =>
      row.original.expiry_date ? (
        <div className="flex flex-wrap items-center gap-2 whitespace-nowrap">
          <span className="text-on-surface-variant tabular-nums">{plainDateLabel(row.original.expiry_date)}</span>
          {row.original.is_expired && <ExpiredBadge />}
        </div>
      ) : (
        <span className="text-outline">—</span>
      ),
  },
  {
    accessorKey: 'quantity',
    header: 'Quantity',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-semibold tabular-nums">
        {quantityLabel(row.original.quantity, row.original.unit_of_measure)}
      </span>
    ),
  },
]

const stockColumnsWithActions: ColumnDef<StockRow>[] = [
  ...stockColumns,
  {
    id: 'actions',
    header: '',
    enableSorting: false,
    // The table keeps a cell by its row's position, not by its stock row. Keyed
    // by the row, an open dialog is closed if a refetch puts another batch in
    // this place — it can never carry on against stock it was not opened for.
    cell: ({ row }) => <RowActions key={row.original.id} row={row.original} />,
  },
]

/**
 * Stock on the shelf (`GET /inventory/stock`, which needs
 * `inventory.stock.read`): one row per batch of an item at a location.
 *
 * The filters are the API's: one item, one location, and whether rows holding
 * nothing are listed (they are left out unless asked for — `in_stock_only`
 * defaults to true on the server, so it is sent only as `false`). The item and
 * location live in the address (`?item_id=`, `?location_id=`), which is how
 * the overview links here.
 */
export default function StockPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own: it is not
  // made for someone the endpoint would refuse, whatever the page is mounted under.
  const canRead = can('inventory.stock.read')
  const canConsume = can('inventory.consume')
  const canTransfer = can('inventory.transfer')
  const canAdjust = can('inventory.adjust')
  const [searchParams, setSearchParams] = useSearchParams()
  const itemId = idParam(searchParams.get('item_id'))
  const locationId = idParam(searchParams.get('location_id'))
  const [includeEmpty, setIncludeEmpty] = useState(false)
  const [picked, setPicked] = useState<InventoryItemRef | null>(null)
  // The page belongs to the filters it was chosen under. Two of them are the
  // address bar's and change under a mounted page; a page carried across would
  // ask for page 3 of one item's two rows and report none.
  const scopeKey = `${itemId ?? ''}|${locationId ?? ''}|${includeEmpty}`
  const [paged, setPaged] = useState({ scopeKey, page: 1 })
  const page = paged.scopeKey === scopeKey ? paged.page : 1
  const setPage = (next: number) => setPaged({ scopeKey, page: next })

  const { data, isPending, isError, isPlaceholderData, refetch } = useStock(
    {
      item_id: itemId,
      location_id: locationId,
      in_stock_only: includeEmpty ? false : undefined,
      page,
      page_size: PAGE_SIZE,
    },
    { enabled: canRead },
  )

  // While a new filter loads, the rows on hand are the previous request's and
  // may be another item's. They are not shown under this filter's banner.
  const inScope = (row: StockRow) =>
    (!itemId || row.item_id === itemId) && (!locationId || row.location_id === locationId)
  const rows = (data?.items ?? []).filter((row) => !isPlaceholderData || inScope(row))
  const meta = data?.pagination
  const filtered = !!itemId || !!locationId

  const lastPage = Math.max(1, meta?.totalPages ?? 1)
  const pastLastPage = !!meta && !isPlaceholderData && page > lastPage
  if (pastLastPage) setPage(lastPage)

  // Naming the filters. A stock row names its own item, and so does a pick
  // from the list; only a bare id from the address bar with no rows to show
  // needs the item read (`inventory.item.read`).
  const fromRows = itemId ? rows.find((row) => row.item_id === itemId) : undefined
  const knownItem = picked?.id === itemId ? picked : fromRows ? itemRefOf(fromRows) : null
  const fetchedItem = useItem(itemId, {
    // Not before the rows have answered: they usually name the item themselves.
    enabled: can('inventory.item.read') && !!itemId && !knownItem && !!data && !isPlaceholderData,
  })
  const item = knownItem ?? (fetchedItem.data?.id === itemId ? fetchedItem.data : null) ?? null
  const locations = useLocations({}, { enabled: can('inventory.location.read') && !!locationId })
  const locationName =
    rows.find((row) => row.location_id === locationId)?.location_name ??
    locations.data?.find((location) => location.id === locationId)?.name

  const setFilter = (name: 'item_id' | 'location_id', value: string | undefined) =>
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        if (value) params.set(name, value)
        else params.delete(name)
        return params
      },
      { replace: true },
    )
  const clearFilters = () =>
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        params.delete('item_id')
        params.delete('location_id')
        return params
      },
      { replace: true },
    )

  if (!canRead) {
    return (
      <div className="w-full">
        <InventoryTabs />
        <Alert variant="error" title="You can't view stock">
          Your account is not permitted to see stock levels. If that has just changed, sign in again.
        </Alert>
      </div>
    )
  }

  return (
    <div className="w-full">
      <InventoryTabs />
      <PageHeader
        title="Stock"
        subtitle="What each location holds, batch by batch. Expired stock stays listed until it is written off, and is never used."
        actions={
          <>
            {canConsume && (
              <ConsumeDialog
                trigger={
                  <Button className="rounded-full">
                    <PackageMinus className="size-4" /> Record stock used
                  </Button>
                }
              />
            )}
            {canTransfer && (
              <TransferDialog
                trigger={
                  <Button variant="outline" className="rounded-full">
                    <ArrowLeftRight className="size-4" /> Transfer stock
                  </Button>
                }
              />
            )}
            {canAdjust && (
              <AdjustDialog
                trigger={
                  <Button variant="outline" className="rounded-full">
                    <SlidersHorizontal className="size-4" /> Adjust stock
                  </Button>
                }
              />
            )}
          </>
        }
      />

      {filtered && (
        <Alert variant="info" title="Showing part of the stock" className="mb-6">
          <div className="flex flex-col items-start gap-3">
            <p className="[overflow-wrap:anywhere]">
              Only{' '}
              {itemId && (
                <span className="text-on-surface font-semibold">{item ? item.name : 'one item'}</span>
              )}
              {!itemId && 'stock'}
              {locationId && (
                <>
                  {' '}
                  at <span className="text-on-surface font-semibold">{locationName ?? 'one location'}</span>
                </>
              )}
              {' '}
              is listed.
            </p>
            <Button variant="outline" size="sm" onClick={clearFilters}>
              Show all stock
            </Button>
          </div>
        </Alert>
      )}

      <div className="mb-6 grid gap-4 md:grid-cols-3">
        <ItemFilter
          itemId={itemId}
          item={item}
          onPick={(next) => {
            setPicked(next)
            setFilter('item_id', next.id)
          }}
          onClear={() => setFilter('item_id', undefined)}
        />
        <Field label="Location">
          {(p) => (
            <LocationSelect
              id={p.id}
              value={locationId ?? ''}
              onChange={(id) => setFilter('location_id', id || undefined)}
              allLabel="All locations"
            />
          )}
        </Field>
        <label className="font-body text-body-sm text-on-surface flex items-start gap-2 md:pt-8">
          <Checkbox
            className="mt-0.5"
            checked={includeEmpty}
            onCheckedChange={(v) => setIncludeEmpty(v === true)}
          />
          <span>
            Include empty rows
            <span className="text-outline block text-xs">Batches and places that now hold nothing.</span>
          </span>
        </label>
      </div>

      {isError ? (
        <Alert variant="error" title="Couldn't load the stock">
          <div className="flex flex-col items-start gap-3">
            <p>The stock could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={canConsume || canTransfer || canAdjust ? stockColumnsWithActions : stockColumns}
          data={rows}
          isLoading={isPending || (isPlaceholderData && rows.length === 0)}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState
                icon={Boxes}
                title="No stock matches"
                description={
                  includeEmpty
                    ? 'There is no stock row for this item and location.'
                    : 'Nothing is held for this item and location. Rows holding nothing are hidden — tick Include empty rows to list them.'
                }
              />
            ) : (
              <EmptyState
                icon={Boxes}
                title="No stock yet"
                description={
                  includeEmpty
                    ? 'No stock has been recorded at any location. It appears here when an order is received or an opening balance is entered as an adjustment.'
                    : 'No location holds any stock. It appears here when an order is received or an opening balance is entered as an adjustment.'
                }
              />
            )
          }
        />
      )}
    </div>
  )
}
