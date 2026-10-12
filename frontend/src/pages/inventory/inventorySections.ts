import type { Permission } from '@/lib/rbac'

/**
 * The Inventory screens, each with the permission its list endpoint requires
 * (docs/18-API_CONTRACTS.md §10.1). `end` marks a path that is also the prefix
 * of the others, so its tab is active on that exact path only.
 */
export const INVENTORY_SECTIONS: { to: string; label: string; permission: Permission; end?: boolean }[] = [
  { to: '/inventory', label: 'Overview', permission: 'inventory.stock.read', end: true },
  { to: '/inventory/stock', label: 'Stock', permission: 'inventory.stock.read' },
  { to: '/inventory/movements', label: 'Movements', permission: 'inventory.stock.read' },
  { to: '/inventory/items', label: 'Items', permission: 'inventory.item.read' },
  { to: '/inventory/locations', label: 'Locations', permission: 'inventory.location.read' },
  { to: '/inventory/purchase-orders', label: 'Purchase orders', permission: 'inventory.po.read' },
]

/** The first Inventory screen this user may open, or undefined if there is none. */
export function firstInventorySection(can: (permission: Permission) => boolean): string | undefined {
  return INVENTORY_SECTIONS.find((s) => can(s.permission))?.to
}
