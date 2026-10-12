import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { ColumnDef } from '@tanstack/react-table'
import { Boxes, RotateCw } from 'lucide-react'
import PageHeader from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { DataTable } from '@/components/ui/data-table'
import { EmptyState } from '@/components/ui/empty-state'
import { Field } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { useStockSummary, type ItemStockSummary } from '@/api/inventory'
import { LowStockBadge } from '@/components/inventory/InventoryBadges'
import { quantityLabel } from '@/components/inventory/inventoryPresentation'
import { usePermissions } from '@/hooks/usePermissions'
import { InventoryTabs } from './InventoryTabs'

const PAGE_SIZE = 25
/** The API's limits on `q` and `category`; a longer one is a 422, not a search that finds nothing. */
const MAX_QUERY = 200
const MAX_CATEGORY = 100
/** Why a search can find nothing, in the API's own matching rules. */
const NO_MATCH =
  'Search matches the start of a name, or a whole SKU — not a word in the middle. A category must match exactly, capitals included. Inactive items are not listed.'

/**
 * Per-item columns. Sorting is off: the API returns items by name and that
 * order is kept. Every figure and the low flag are the server's
 * (docs/18-API_CONTRACTS.md §10.4) — nothing here is added up or compared.
 */
const columns: ColumnDef<ItemStockSummary>[] = [
  {
    id: 'item',
    header: 'Item',
    enableSorting: false,
    cell: ({ row }) => (
      <div className="flex max-w-72 min-w-40 flex-col [overflow-wrap:anywhere]">
        <Link
          to={`/inventory/stock?item_id=${row.original.item.id}`}
          aria-label={`Stock of ${row.original.item.name}`}
          className="text-on-surface hover:text-secondary focus-visible:ring-secondary rounded font-semibold transition-colors outline-none focus-visible:ring-2"
        >
          {row.original.item.name}
        </Link>
        <span className="text-outline font-mono text-xs">{row.original.item.sku}</span>
      </div>
    ),
  },
  {
    id: 'category',
    header: 'Category',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant block max-w-40 [overflow-wrap:anywhere]">
        {row.original.item.category ?? '—'}
      </span>
    ),
  },
  {
    accessorKey: 'quantity_on_hand',
    header: 'On hand',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="text-on-surface-variant tabular-nums">
        {quantityLabel(row.original.quantity_on_hand, row.original.item.unit_of_measure)}
      </span>
    ),
  },
  {
    // `usable_quantity`: what is held and has not expired (schemas/inventory.py).
    accessorKey: 'usable_quantity',
    header: 'Usable (not expired)',
    enableSorting: false,
    cell: ({ row }) => (
      <span className="font-semibold tabular-nums">
        {quantityLabel(row.original.usable_quantity, row.original.item.unit_of_measure)}
      </span>
    ),
  },
  {
    accessorKey: 'is_low',
    header: 'Status',
    enableSorting: false,
    cell: ({ row }) => <LowStockBadge isLow={row.original.is_low} />,
  },
  {
    // The server sends "0.00" for an item that is not low; that is not a
    // suggestion to order nothing, so it is shown only on a low row.
    accessorKey: 'suggested_order_quantity',
    header: 'Suggested order',
    enableSorting: false,
    cell: ({ row }) =>
      row.original.is_low ? (
        <span className="tabular-nums">
          {quantityLabel(row.original.suggested_order_quantity, row.original.item.unit_of_measure)}
        </span>
      ) : (
        <span className="text-outline">—</span>
      ),
  },
]

/**
 * Inventory at a glance (`GET /inventory/stock/summary`, which needs
 * `inventory.stock.read`): each active item's stock across the whole hospital,
 * by name. This is the page the `inventory.low_stock` notification opens.
 *
 * Search is the API's: the start of a name, or a whole SKU; category is an
 * exact match. Neither is trimmed by the server, so both are trimmed here.
 * "Low stock only" is the API's `low_stock=true` and is kept in the address
 * (`?low_stock=1`) so the reorder list can be linked to.
 *
 * Whether an item is low, and how much to order, are the server's answers.
 * There is no forecast: the API has none.
 */
export default function InventoryOverviewPage() {
  const { can } = usePermissions()
  // The route guards this page, but the request is the page's own: it is not
  // made for someone the endpoint would refuse, whatever the page is mounted under.
  const canRead = can('inventory.stock.read')
  const [searchParams, setSearchParams] = useSearchParams()
  const lowOnly = searchParams.get('low_stock') === '1'
  const [search, setSearch] = useState('')
  const [categoryText, setCategoryText] = useState('')
  // The filters and the page they are read at change together: a new term
  // starts at page 1 in the same request, never as a page 1 of the old term first.
  const [query, setQuery] = useState({ q: '', category: '', lowOnly, page: 1 })
  const { q, category } = query
  // The toggle is the address bar's, so it can change under a mounted page.
  const page = query.lowOnly === lowOnly ? query.page : 1
  const setPage = (next: number) => setQuery((current) => ({ ...current, lowOnly, page: next }))

  useEffect(() => {
    const id = setTimeout(() => {
      const term = search.trim().slice(0, MAX_QUERY)
      const exact = categoryText.trim().slice(0, MAX_CATEGORY)
      // Typing that leaves both as they were — spaces — is not a new search
      // and keeps the page.
      setQuery((current) =>
        current.q === term && current.category === exact ? current : { ...current, q: term, category: exact, page: 1 },
      )
    }, 300)
    return () => clearTimeout(id)
  }, [search, categoryText])

  const { data, isPending, isError, isPlaceholderData, refetch } = useStockSummary(
    {
      // An empty filter is left out: the API reads `low_stock=false` as the
      // default and has no use for an empty term.
      low_stock: lowOnly ? true : undefined,
      q: q || undefined,
      category: category || undefined,
      page,
      page_size: PAGE_SIZE,
    },
    { enabled: canRead },
  )

  const summaries = data?.items ?? []
  const meta = data?.pagination

  // The list can shrink under the page being read — a delivery lifts an item
  // off the low list — and the API then answers a page past the last one with
  // no rows. That is not "nothing matches": go to the last page there is.
  const lastPage = Math.max(1, meta?.totalPages ?? 1)
  const pastLastPage = !!meta && !isPlaceholderData && page > lastPage
  if (pastLastPage) setPage(lastPage)

  const setLowOnly = (next: boolean) =>
    setSearchParams(
      (current) => {
        const params = new URLSearchParams(current)
        if (next) params.set('low_stock', '1')
        else params.delete('low_stock')
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
        title="Inventory"
        subtitle="What the hospital holds of each active item, across every location."
      />

      <p className="font-body text-body-sm text-on-surface-variant mb-6 max-w-3xl">
        Low stock is judged by the server, not by this page: an item is low when its usable stock across the
        whole hospital is at or below the item's reorder point. An item with no reorder point is never low.
        The suggested order is the server's too — what would bring usable stock back to the item's target
        stock, or to its reorder point when no target is set.
      </p>

      {isError ? (
        <Alert variant="error" title="Couldn't load the stock summary">
          <div className="flex flex-col items-start gap-3">
            <p>The summary could not be reached. Check your connection and try again.</p>
            <Button variant="outline" size="sm" onClick={() => refetch()}>
              <RotateCw className="size-4" /> Retry
            </Button>
          </div>
        </Alert>
      ) : (
        <DataTable
          columns={columns}
          data={summaries}
          // Rows kept from the previous request while the next one loads are
          // worth showing; an empty result kept that way is not — it would
          // say "nothing matches" about a request that has not answered.
          isLoading={isPending || (isPlaceholderData && summaries.length === 0)}
          searchable
          searchPlaceholder="Search by start of name or whole SKU…"
          searchValue={search}
          onSearchChange={setSearch}
          toolbarRight={
            <div className="flex flex-wrap items-end gap-4">
              {/* A visible label: once something is typed, a placeholder no longer says what the box filters. */}
              <Field label="Category" hint="Exactly as on the item, capitals included" className="w-64 max-w-full">
                {(p) => (
                  <Input
                    {...p}
                    value={categoryText}
                    onChange={(e) => setCategoryText(e.target.value)}
                    autoComplete="off"
                  />
                )}
              </Field>
              <label className="font-body text-body-sm text-on-surface flex items-center gap-2">
                <Checkbox checked={lowOnly} onCheckedChange={(v) => setLowOnly(v === true)} />
                Low stock only
              </label>
            </div>
          }
          pageSize={PAGE_SIZE}
          serverPagination={
            meta ? { page: meta.page, totalPages: meta.totalPages, onPageChange: setPage } : undefined
          }
          emptyState={
            q || category ? (
              <EmptyState
                icon={Boxes}
                title="No matching items"
                description={
                  // With the low list on, an item that exists but is not low is
                  // not found either — which must not read as "no such item".
                  lowOnly
                    ? `${NO_MATCH} Only items the server reports as low are listed — untick Low stock only to search every item.`
                    : NO_MATCH
                }
              />
            ) : lowOnly ? (
              <EmptyState
                icon={Boxes}
                title="No item is low on stock"
                description="The server reports no active item at or below its reorder point."
              />
            ) : (
              <EmptyState
                icon={Boxes}
                title="No items yet"
                description="Active items in the catalog appear here, with what the hospital holds of each."
              />
            )
          }
        />
      )}
    </div>
  )
}
