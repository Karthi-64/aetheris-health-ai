import { X } from 'lucide-react'
import { Field } from '@/components/ui/field'
import type { InventoryItem, InventoryItemRef } from '@/api/inventory'
import { ItemPicker } from '@/components/inventory/ItemPicker'

/**
 * The item filter of the stock list and of the ledger. Both endpoints filter
 * by `item_id` (docs/18-API_CONTRACTS.md §10.4, §10.5), so an item is chosen
 * with the picker and then shown as a chip until it is cleared.
 *
 * The chip is this component's, not the picker's: a filter can arrive as a
 * bare id in the address bar, before anything knows the item's name, and the
 * picker would answer that by listing the catalog. While the name is unknown
 * the chip says so rather than showing an id.
 */
export function ItemFilter({
  itemId,
  item,
  onPick,
  onClear,
}: {
  /** The id being filtered by, if any. */
  itemId: string | undefined
  /** What is known about that item — null until it is, or if it cannot be read. */
  item: InventoryItemRef | null
  onPick: (item: InventoryItem) => void
  onClear: () => void
}) {
  return (
    <Field label="Item">
      {(p) =>
        itemId ? (
          // The label above points at this id. A chip is not a form control, so
          // it is named as a group — otherwise the label would name nothing
          // once an item is chosen.
          <div
            id={p.id}
            role="group"
            aria-label={`Item filter: ${item ? item.name : 'one item'}`}
            className="neo-pressed bg-surface flex items-center justify-between gap-3 rounded-xl px-4 py-2.5"
          >
            <span className="font-body text-body-sm text-on-surface min-w-0 [overflow-wrap:anywhere]">
              {item ? (
                <>
                  {item.name}
                  <span className="text-on-surface-variant"> · {item.unit_of_measure}</span>{' '}
                  <span className="text-outline font-mono text-xs">{item.sku}</span>
                </>
              ) : (
                'One item'
              )}
            </span>
            <button
              type="button"
              onClick={onClear}
              aria-label="Clear the item filter"
              className="text-outline hover:text-error focus-visible:ring-secondary shrink-0 rounded transition-colors outline-none focus-visible:ring-2"
            >
              <X className="size-4" />
            </button>
          </div>
        ) : (
          <ItemPicker id={p.id} value="" onChange={(picked) => picked && onPick(picked)} />
        )
      }
    </Field>
  )
}
