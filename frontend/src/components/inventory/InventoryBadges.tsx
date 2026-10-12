import { Badge } from '@/components/ui/badge'
import type { InventoryLocationKind, InventoryOrderStatus, MovementReason } from '@/api/inventory'
import {
  INVENTORY_ORDER_STATUS,
  LOCATION_KIND,
  MOVEMENT_REASON,
} from './inventoryPresentation'

export function InventoryOrderStatusBadge({ status }: { status: InventoryOrderStatus }) {
  const s = INVENTORY_ORDER_STATUS[status]
  return <Badge variant={s.variant}>{s.label}</Badge>
}

/**
 * Whether an item is low, from the server's `is_low` (docs/18-API_CONTRACTS.md
 * §10.4) — never worked out here. The word carries the state; the colour only
 * repeats it.
 */
export function LowStockBadge({ isLow }: { isLow: boolean }) {
  return <Badge variant={isLow ? 'warning' : 'success'}>{isLow ? 'Low' : 'OK'}</Badge>
}

export function MovementReasonBadge({ reason }: { reason: MovementReason }) {
  const r = MOVEMENT_REASON[reason]
  return <Badge variant={r.variant}>{r.label}</Badge>
}

export function LocationKindBadge({ kind }: { kind: InventoryLocationKind }) {
  return <Badge variant="neutral">{LOCATION_KIND[kind].label}</Badge>
}

/** Marks a stock row the server reports as `is_expired`: on the shelf, never used. Render it only for such a row. */
export function ExpiredBadge() {
  return <Badge variant="critical">Expired</Badge>
}
