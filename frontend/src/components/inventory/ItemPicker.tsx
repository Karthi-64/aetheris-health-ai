import { useEffect, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useItems, type InventoryItem, type InventoryItemRef } from '@/api/inventory'
import { usePermissions } from '@/hooks/usePermissions'
import { cn } from '@/lib/utils'

/** How many matches to offer at a time. The search narrows them. */
const MATCHES = 8

/**
 * Searchable picker over the active inventory items (`GET /inventory/items`,
 * which needs `inventory.item.read`; without it nothing is requested). Shows
 * the chosen item as a chip.
 *
 * Search is the API's: the start of a name, or a whole SKU. `value` is the
 * chosen item's id; the whole record is handed to `onChange` so a caller can
 * read its unit or whether it is batch-tracked without a second request.
 */
export function ItemPicker({
  value,
  onChange,
  invalid,
  id,
  excludeIds = [],
  initialItem,
}: {
  value: string
  onChange: (item: InventoryItem | null) => void
  invalid?: boolean
  id?: string
  /** Items already chosen on another line. Listed, but cannot be picked twice. */
  excludeIds?: readonly string[]
  /**
   * The item `value` names when the caller already knows it — a dialog opened
   * from a stock row — so the chip shows without a request.
   */
  initialItem?: InventoryItemRef | null
}) {
  const { can } = usePermissions()
  const canRead = can('inventory.item.read')
  const [search, setSearch] = useState('')
  const [q, setQ] = useState('')
  const [picked, setPicked] = useState<InventoryItemRef | null>(null)

  useEffect(() => {
    const t = setTimeout(() => setQ(search.trim()), 250)
    return () => clearTimeout(t)
  }, [search])

  // Matched on id, so a form reset or a dialog reopened for another row never
  // shows the previous choice against the new value.
  const chosen = !value ? null : picked?.id === value ? picked : initialItem?.id === value ? initialItem : null

  const { data, isError } = useItems(
    { q: q || undefined, is_active: true, page: 1, page_size: MATCHES },
    { enabled: canRead && !chosen },
  )
  const results = data?.items ?? []

  if (chosen) {
    return (
      <div className="neo-pressed bg-surface flex items-center justify-between gap-3 rounded-xl px-4 py-2.5">
        <span className="font-body text-body-sm text-on-surface min-w-0 [overflow-wrap:anywhere]">
          {chosen.name}
          <span className="text-on-surface-variant"> · {chosen.unit_of_measure}</span>{' '}
          <span className="text-outline font-mono text-xs">{chosen.sku}</span>
        </span>
        <button
          type="button"
          onClick={() => {
            setPicked(null)
            onChange(null)
          }}
          aria-label={`Change item (${chosen.name})`}
          className="text-outline hover:text-error focus-visible:ring-secondary shrink-0 rounded transition-colors outline-none focus-visible:ring-2"
        >
          <X className="size-4" />
        </button>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      <div className="relative">
        <Search className="text-outline pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2" />
        <input
          id={id}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search by name or exact SKU…"
          autoComplete="off"
          aria-invalid={invalid}
          disabled={!canRead}
          className={cn(
            'neo-pressed bg-surface font-body text-body-sm text-on-surface placeholder:text-outline-variant focus-visible:ring-secondary w-full rounded-xl py-2.5 pr-4 pl-9 outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-60',
            invalid && 'ring-error ring-2',
          )}
        />
      </div>
      {!canRead ? (
        <p className="font-body text-outline text-xs">You don't have access to the item list.</p>
      ) : isError ? (
        <p className="font-body text-error text-xs" role="alert">
          The item list couldn't be loaded.
        </p>
      ) : results.length > 0 ? (
        <ul className="neo-extruded bg-surface max-h-44 overflow-y-auto rounded-xl p-1">
          {results.map((item) => {
            const taken = excludeIds.includes(item.id)
            return (
              <li key={item.id}>
                <button
                  type="button"
                  disabled={taken}
                  title={taken ? 'Already on this list' : undefined}
                  onClick={() => {
                    setPicked(item)
                    onChange(item)
                  }}
                  className="hover:bg-secondary/10 focus-visible:ring-secondary flex w-full items-center justify-between gap-3 rounded-lg px-3 py-2 text-left transition-colors outline-none focus-visible:ring-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent"
                >
                  <span className="font-body text-body-sm text-on-surface min-w-0 [overflow-wrap:anywhere]">
                    {item.name}
                    <span className="text-on-surface-variant"> · {item.unit_of_measure}</span>
                    {taken && <span className="text-outline"> — already added</span>}
                  </span>
                  <span className="text-outline shrink-0 font-mono text-xs">{item.sku}</span>
                </button>
              </li>
            )
          })}
        </ul>
      ) : data && q ? (
        <p className="font-body text-outline text-xs">
          No active item matches “{q}”. Search matches the start of a name, or a whole SKU.
        </p>
      ) : data ? (
        <p className="font-body text-outline text-xs">There are no active items yet.</p>
      ) : null}
    </div>
  )
}
