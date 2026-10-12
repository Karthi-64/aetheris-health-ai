import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { ChevronRight, Package, Pencil, Plus, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Alert } from '@/components/ui/alert'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useItems, type InventoryItem } from '@/api/inventory'
import { usePermissions } from '@/hooks/usePermissions'
import { CreateItemDialog, EditItemDialog } from './ItemDialog'
import { InventoryTabs } from './InventoryTabs'

const PAGE_SIZE = 25
const ALL = 'all'
/**
 * The API's limits on `q` and `category`; a longer one is a 422, not a search
 * that finds nothing. Both boxes stop at the limit, so what is searched is
 * always what is shown in them.
 */
const MAX_QUERY = 200
const MAX_CATEGORY = 100

/** One of an item's own settings, or a dash when it has none. Never a stock figure. */
function Level({ value }: { value: number | null }) {
  return value === null ? (
    <span className="text-outline-variant">—</span>
  ) : (
    <span className="tabular-nums">{value}</span>
  )
}

/**
 * The item catalog (docs/18-API_CONTRACTS.md §10.3), ordered by name by the
 * API.
 *
 * Search is the API's: the start of a name, or a whole SKU. The category is
 * matched exactly, capitals included. The API trims neither, so both are
 * trimmed here, and a blank one is left out of the request — an empty
 * `category` would be matched literally and find nothing. With no status
 * chosen the API returns active and inactive items alike.
 *
 * No stock figure is shown: an item carries none, and a count per row would be
 * a request per row. The reorder point and target are the item's own settings,
 * not its stock — whether it is low is the server's judgement and is shown on
 * the stock screens. Stock is one click away for those who hold
 * `inventory.stock.read`. Adding and editing need `inventory.item.create` and
 * `inventory.item.update`. There is no delete.
 */
export default function ItemsPage() {
  const { can } = usePermissions()
  const canCreate = can('inventory.item.create')
  const canEdit = can('inventory.item.update')
  const canReadStock = can('inventory.stock.read')
  const [search, setSearch] = useState('')
  const [categoryText, setCategoryText] = useState('')
  // The filters and the page they are read at change together: a new term
  // starts at page 1 in the same request, never as a page 1 of the old term first.
  const [{ q, category, page }, setQuery] = useState({ q: '', category: '', page: 1 })
  const [active, setActive] = useState<string>(ALL)
  const setPage = (next: number) => setQuery((current) => ({ ...current, page: next }))

  useEffect(() => {
    const id = setTimeout(() => {
      const term = search.trim()
      const exact = categoryText.trim()
      // Typing that leaves both as they were — spaces — is not a new search
      // and keeps the page.
      setQuery((current) =>
        current.q === term && current.category === exact ? current : { q: term, category: exact, page: 1 },
      )
    }, 300)
    return () => clearTimeout(id)
  }, [search, categoryText])

  const { data, isPending, isError, isPlaceholderData, refetch } = useItems({
    q: q || undefined,
    category: category || undefined,
    is_active: active === ALL ? undefined : active === 'active',
    page,
    page_size: PAGE_SIZE,
  })

  const items = data?.items ?? []
  const meta = data?.pagination
  const filtered = !!q || !!category || active !== ALL
  // Every filter in force is named: with several on, any one of them may be
  // what leaves the list empty.
  const whyNoMatch = [
    q && 'Search matches the start of a name, or a whole SKU — not a word in the middle, and not part of a SKU.',
    category && 'A category matches only when it is written exactly as on the item, capitals included.',
    active !== ALL &&
      (q || category ? `Only ${active} items are being shown.` : 'No item in the catalog has this status.'),
  ]
    .filter(Boolean)
    .join(' ')

  // The catalog can shrink under the page being read — an item switched off
  // while "Active only" is on, say — and the API then answers a page past the
  // last one with no rows. That is not "nothing matches": go to the last page
  // there is. A page kept on screen from the previous request says nothing
  // about this one, so it is not judged.
  const lastPage = Math.max(1, meta?.totalPages ?? 1)
  const pastLastPage = !!meta && !isPlaceholderData && page > lastPage
  if (pastLastPage) setPage(lastPage)

  const columns: ColumnDef<InventoryItem>[] = useMemo(
    () => [
      {
        accessorKey: 'name',
        header: 'Item',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface block max-w-72 min-w-40 font-semibold [overflow-wrap:anywhere]">
            {row.original.name}
          </span>
        ),
      },
      {
        accessorKey: 'sku',
        header: 'SKU',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant block max-w-40 font-mono text-xs [overflow-wrap:anywhere]">
            {row.original.sku}
          </span>
        ),
      },
      {
        accessorKey: 'category',
        header: 'Category',
        enableSorting: false,
        cell: ({ row }) =>
          row.original.category ? (
            <span className="text-on-surface-variant block max-w-48 min-w-24 [overflow-wrap:anywhere]">
              {row.original.category}
            </span>
          ) : (
            <span className="text-outline-variant">—</span>
          ),
      },
      {
        accessorKey: 'unit_of_measure',
        header: 'Unit',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant block max-w-40 [overflow-wrap:anywhere]">
            {row.original.unit_of_measure}
          </span>
        ),
      },
      {
        accessorKey: 'is_batch_tracked',
        header: 'Batch tracked',
        enableSorting: false,
        cell: ({ row }) => (
          <span className="text-on-surface-variant">{row.original.is_batch_tracked ? 'Yes' : 'No'}</span>
        ),
      },
      {
        accessorKey: 'reorder_point',
        header: 'Reorder point (setting)',
        enableSorting: false,
        cell: ({ row }) => <Level value={row.original.reorder_point} />,
      },
      {
        accessorKey: 'target_stock',
        header: 'Target stock (setting)',
        enableSorting: false,
        cell: ({ row }) => <Level value={row.original.target_stock} />,
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
              // item, an open form is closed if a refetch puts another item in
              // this row, rather than saving its values onto it.
              <EditItemDialog
                key={row.original.id}
                item={row.original}
                trigger={
                  <Button variant="ghost" size="sm" aria-label={`Edit ${row.original.name}`}>
                    <Pencil className="size-4" /> Edit
                  </Button>
                }
              />
            )}
            {canReadStock && (
              <Link
                to={`/inventory/stock?item_id=${row.original.id}`}
                aria-label={`Stock of ${row.original.name}`}
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
      <Plus className="size-4" /> Add item
    </Button>
  )

  return (
    <div className="w-full">
      <InventoryTabs />
      <PageHeader
        title="Items"
        subtitle="The catalog of consumables and supplies the hospital stocks. The reorder point and target are each item's own settings; stock itself is on the Stock screen."
        actions={
          <>
            <Input
              value={categoryText}
              onChange={(e) => setCategoryText(e.target.value.slice(0, MAX_CATEGORY))}
              maxLength={MAX_CATEGORY}
              aria-label="Filter by exact category"
              placeholder="Category, exactly as written"
              autoComplete="off"
              className="w-56 rounded-full py-2.5"
            />
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
            {canCreate && <CreateItemDialog trigger={addButton} />}
          </>
        }
      />

      {isError ? (
        <Alert variant="error" title="Couldn't load the item catalog">
          <div className="flex flex-col items-start gap-3">
            <p>The catalog could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={items}
          // Rows kept from the previous request while the next one loads are
          // worth showing; an empty result kept that way is not — it would
          // say "nothing matches" about a request that has not answered.
          isLoading={isPending || (isPlaceholderData && items.length === 0)}
          searchable
          searchPlaceholder="Search by start of name or whole SKU…"
          searchValue={search}
          // The shared table's search box takes no maxLength, so the cap is
          // put on the value it is given back.
          onSearchChange={(value) => setSearch(value.slice(0, MAX_QUERY))}
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            filtered ? (
              <EmptyState icon={Package} title="No matching items" description={whyNoMatch} />
            ) : (
              <EmptyState
                icon={Package}
                title="No items in the catalog"
                description={
                  canCreate
                    ? 'Add the consumables and supplies the hospital stocks so their stock can be recorded, used and ordered.'
                    : 'Items added to the catalog will appear here.'
                }
                action={canCreate ? <CreateItemDialog trigger={addButton} /> : undefined}
              />
            )
          }
        />
      )}
    </div>
  )
}
