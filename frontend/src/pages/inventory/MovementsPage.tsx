import { createContext, useContext, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { History, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Field } from '@/components/ui/field'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useDepartments } from '@/api/departments'
import {
  useItem,
  useItems,
  useLocations,
  useMovements,
  type InventoryItem,
  type InventoryItemRef,
  type MovementReason,
  type StockMovement,
} from '@/api/inventory'
import { MovementReasonBadge } from '@/components/inventory/InventoryBadges'
import {
  MOVEMENT_REASON,
  MOVEMENT_REASONS,
  isRemoval,
  signedQuantityLabel,
} from '@/components/inventory/inventoryPresentation'
import { usePermissions } from '@/hooks/usePermissions'
import { formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'
import { InventoryTabs } from './InventoryTabs'
import { LocationSelect } from './StockActions'
import { ItemFilter } from './StockPageFilters'
import { idParam, referenceLabel } from './stockActionForms'

const PAGE_SIZE = 25
const ALL = 'all'
/** The most the item list returns in one page — enough to name every item of most catalogs at once. */
const NAMED_AT_ONCE = 100

/**
 * A ledger entry names its item and its location by id only
 * (`MovementResponse`), so the names are looked up: the locations from the
 * whole list, the items from the first page of the catalog. The cells read
 * them from here, which keeps the columns constant.
 */
interface Names {
  items: Map<string, InventoryItemRef>
  /** The catalog page has answered (or failed), so a missing item is really missing from it. */
  itemsSettled: boolean
  canReadItems: boolean
  locations: Map<string, string>
}

const LedgerNames = createContext<Names>({
  items: new Map(),
  itemsSettled: false,
  canReadItems: false,
  locations: new Map(),
})

/**
 * The item an entry is about. An item beyond the first page of the catalog is
 * read by itself (`GET /inventory/items/{id}`) — once, however many entries
 * name it.
 */
function useLedgerItem(id: string): { item: InventoryItemRef | undefined; pending: boolean } {
  const { items, itemsSettled, canReadItems } = useContext(LedgerNames)
  const listed = items.get(id)
  const { data, isPending } = useItem(id, { enabled: canReadItems && itemsSettled && !listed })
  return {
    item: listed ?? data,
    // Still being looked up — in the catalog page, then by itself. Only once
    // both have answered is an item that has no name really unavailable.
    pending: canReadItems && !listed && !data && (!itemsSettled || isPending),
  }
}

function ItemCell({ movement }: { movement: StockMovement }) {
  const { item, pending } = useLedgerItem(movement.item_id)
  // A name on its way is not a missing one: say nothing about the data yet.
  if (pending) return <span className="text-outline">…</span>
  if (!item) return <span className="text-outline">Item not available</span>
  return (
    <div className="flex max-w-64 min-w-36 flex-col [overflow-wrap:anywhere]">
      <span className="text-on-surface font-semibold">{item.name}</span>
      <span className="text-outline font-mono text-xs">{item.sku}</span>
    </div>
  )
}

/** The signed change, with the item's unit once it is known. The sign carries the direction, not the colour alone. */
function ChangeCell({ movement }: { movement: StockMovement }) {
  const { item } = useLedgerItem(movement.item_id)
  return (
    <span
      className={cn(
        'font-semibold whitespace-nowrap tabular-nums',
        isRemoval(movement.quantity_change) ? 'text-on-surface' : 'text-stable',
      )}
    >
      {signedQuantityLabel(movement.quantity_change, item?.unit_of_measure)}
    </span>
  )
}

function LocationCell({ movement }: { movement: StockMovement }) {
  const { locations } = useContext(LedgerNames)
  const name = locations.get(movement.location_id)
  return name ? (
    <span className="text-on-surface-variant block max-w-48 min-w-24 [overflow-wrap:anywhere]">{name}</span>
  ) : (
    <span className="text-outline">Location not available</span>
  )
}

/**
 * What caused the entry (`reference_type`). An entry written by receiving a
 * purchase order carries that order's id, so it links to the order for a user
 * who may open one (`inventory.po.read`).
 */
function SourceCell({ movement }: { movement: StockMovement }) {
  const { can } = usePermissions()
  const label = referenceLabel(movement.reference_type)
  if (!label) return <span className="text-outline">—</span>
  if (movement.reference_type === 'purchase_order' && movement.reference_id && can('inventory.po.read')) {
    return (
      <Link
        to={`/inventory/purchase-orders/${movement.reference_id}`}
        className="text-secondary focus-visible:ring-secondary rounded whitespace-nowrap underline-offset-2 outline-none hover:underline focus-visible:ring-2"
      >
        {label}
      </Link>
    )
  }
  return <span className="text-on-surface-variant whitespace-nowrap">{label}</span>
}

/** Mounted only for an entry that names a department, and only for a user who may list them. */
function DepartmentName({ id }: { id: string }) {
  const { data } = useDepartments()
  const name = data?.find((department) => department.id === id)?.name
  return name ? (
    <span className="text-on-surface-variant block max-w-40 [overflow-wrap:anywhere]">{name}</span>
  ) : (
    <span className="text-outline">{data ? 'Department not available' : '…'}</span>
  )
}

/**
 * Ledger columns. Sorting is off: the API returns the newest entry first and
 * has no other order. Only what `MovementResponse` carries is shown — and of
 * that, not `moved_by`, which is a bare user id with no name to show for it.
 */
const ledgerColumns: ColumnDef<StockMovement>[] = [
  {
    accessorKey: 'moved_at',
    header: 'When',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant whitespace-nowrap tabular-nums">
        {formatDateTime(row.original.moved_at)}
      </span>
    ),
  },
  {
    id: 'item',
    header: 'Item',
    enableSorting: false,
    cell: ({ row }) => <ItemCell movement={row.original} />,
  },
  {
    id: 'location',
    header: 'Location',
    enableSorting: false,
    cell: ({ row }) => <LocationCell movement={row.original} />,
  },
  {
    accessorKey: 'reason',
    header: 'Reason',
    enableSorting: false,
    cell: ({ row }) => <MovementReasonBadge reason={row.original.reason} />,
  },
  {
    accessorKey: 'quantity_change',
    header: 'Change',
    enableSorting: false,
    cell: ({ row }) => <ChangeCell movement={row.original} />,
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
    accessorKey: 'reference_type',
    header: 'Source',
    enableSorting: false,
    cell: ({ row }) => <SourceCell movement={row.original} />,
  },
]

const departmentColumn: ColumnDef<StockMovement> = {
  accessorKey: 'department_id',
  header: 'Department',
  enableSorting: false,
  cell: ({ row }) =>
    row.original.department_id ? (
      <DepartmentName id={row.original.department_id} />
    ) : (
      <span className="text-outline">—</span>
    ),
}

const noteColumn: ColumnDef<StockMovement> = {
  accessorKey: 'note',
  header: 'Note',
  enableSorting: false,
  cell: ({ row }) =>
    row.original.note ? (
      <span className="text-on-surface-variant block max-w-72 min-w-40 [overflow-wrap:anywhere]">
        {row.original.note}
      </span>
    ) : (
      <span className="text-outline">—</span>
    ),
}

/**
 * The stock ledger (`GET /inventory/movements`, which needs
 * `inventory.stock.read`), newest first: one entry for every change to stock —
 * a receipt, a use, each half of a transfer, an adjustment, a write-off.
 *
 * The filters are exactly the API's: one item, one location, one reason. It
 * has no date range, no text search and no sort, so none is offered. Nothing
 * here can be edited: a wrong entry is put right by a new one.
 */
export default function MovementsPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own: it is not
  // made for someone the endpoint would refuse, whatever the page is mounted under.
  const canRead = can('inventory.stock.read')
  const canReadItems = can('inventory.item.read')
  const canReadDepartments = can('department.read')
  // An order line or an item links here as ?item_id=…; a pick or a clear on the
  // page replaces that scope.
  const [searchParams, setSearchParams] = useSearchParams()
  const linkedItemId = idParam(searchParams.get('item_id'))
  const [item, setItem] = useState<InventoryItem | null>(null)
  const itemId = item?.id ?? linkedItemId
  const linkedItem = useItem(linkedItemId ?? '', { enabled: canReadItems && !!linkedItemId && !item })
  const dropLink = () => {
    if (linkedItemId) setSearchParams({}, { replace: true })
  }
  const [locationId, setLocationId] = useState('')
  const [reason, setReason] = useState<string>(ALL)
  const [page, setPage] = useState(1)

  const { data, isPending, isError, isPlaceholderData, refetch } = useMovements(
    {
      // "All" leaves the parameter out: sent empty it is a 422, not "no filter".
      item_id: itemId,
      location_id: locationId || undefined,
      reason: reason === ALL ? undefined : (reason as MovementReason),
      page,
      page_size: PAGE_SIZE,
    },
    { enabled: canRead },
  )

  const catalog = useItems({ page: 1, page_size: NAMED_AT_ONCE }, { enabled: canRead && canReadItems })
  const places = useLocations({}, { enabled: canRead && can('inventory.location.read') })
  const names = useMemo<Names>(
    () => ({
      items: new Map<string, InventoryItemRef>([
        ...(catalog.data?.items ?? []).map((listed): [string, InventoryItemRef] => [listed.id, listed]),
        // The item being filtered by is known from the picker, wherever it sits in the catalog.
        ...(item ? [[item.id, item] as [string, InventoryItemRef]] : []),
      ]),
      itemsSettled: catalog.isSuccess || catalog.isError,
      canReadItems,
      locations: new Map((places.data ?? []).map((location) => [location.id, location.name])),
    }),
    [catalog.data, catalog.isSuccess, catalog.isError, canReadItems, item, places.data],
  )

  const movements = data?.items ?? []
  const meta = data?.pagination
  const filtered = !!item || !!locationId || reason !== ALL

  // The API answers a page past the last one with no rows. That is not
  // "nothing matches": go to the last page there is.
  const lastPage = Math.max(1, meta?.totalPages ?? 1)
  const pastLastPage = !!meta && !isPlaceholderData && page > lastPage
  if (pastLastPage) setPage(lastPage)

  // The department an entry names can only be put into words by someone who
  // may list departments (`department.read`); for anyone else the column is
  // left out rather than filled with ids.
  const columns = useMemo(
    () => [...ledgerColumns, ...(canReadDepartments ? [departmentColumn] : []), noteColumn],
    [canReadDepartments],
  )

  if (!canRead) {
    return (
      <div className="w-full">
        <InventoryTabs />
        <Alert variant="error" title="You can't view the stock ledger">
          Your account is not permitted to see stock movements. If that has just changed, sign in again.
        </Alert>
      </div>
    )
  }

  return (
    <div className="w-full">
      <InventoryTabs />
      <PageHeader
        title="Movements"
        subtitle="Every change to stock, newest first. The ledger is a record: nothing in it can be edited or removed."
      />

      <div className="mb-6 grid gap-4 md:grid-cols-3">
        <ItemFilter
          itemId={itemId}
          item={item ?? linkedItem.data ?? null}
          onPick={(next) => {
            setItem(next)
            dropLink()
            setPage(1)
          }}
          onClear={() => {
            setItem(null)
            dropLink()
            setPage(1)
          }}
        />
        <Field label="Location">
          {(p) => (
            <LocationSelect
              id={p.id}
              value={locationId}
              onChange={(id) => {
                setLocationId(id)
                setPage(1)
              }}
              allLabel="All locations"
            />
          )}
        </Field>
        <Field label="Reason">
          {(p) => (
            <Select
              value={reason}
              onValueChange={(next) => {
                setReason(next)
                setPage(1)
              }}
            >
              <SelectTrigger id={p.id}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL}>All reasons</SelectItem>
                {MOVEMENT_REASONS.map((r) => (
                  <SelectItem key={r} value={r}>
                    {MOVEMENT_REASON[r].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </Field>
      </div>

      {isError ? (
        <Alert variant="error" title="Couldn't load the stock ledger">
          <div className="flex flex-col items-start gap-3">
            <p>The ledger could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <LedgerNames.Provider value={names}>
          <DataTable
            columns={columns}
            data={movements}
            isLoading={isPending || (isPlaceholderData && movements.length === 0)}
            pageSize={PAGE_SIZE}
            serverPagination={
              meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
            }
            emptyState={
              filtered ? (
                <EmptyState
                  icon={History}
                  title="No matching movements"
                  description="No ledger entry has this item, location and reason together."
                />
              ) : (
                <EmptyState
                  icon={History}
                  title="No movements yet"
                  description="Every receipt, use, transfer and adjustment is recorded here as it happens."
                />
              )
            }
          />
        </LedgerNames.Provider>
      )}
    </div>
  )
}
