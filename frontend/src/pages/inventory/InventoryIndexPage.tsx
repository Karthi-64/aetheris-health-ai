import { Navigate } from 'react-router-dom'
import { usePermissions } from '@/hooks/usePermissions'
import { firstInventorySection } from './inventorySections'
import InventoryOverviewPage from './InventoryOverviewPage'

/**
 * `/inventory` itself — where the sidebar and the low-stock notification land
 * (docs/18-API_CONTRACTS.md §10.6). Whoever can read stock gets the overview;
 * a role given some other inventory screen without it is sent to that screen.
 */
export default function InventoryIndexPage() {
  const { can } = usePermissions()
  if (can('inventory.stock.read')) return <InventoryOverviewPage />
  return <Navigate to={firstInventorySection((p) => p !== 'inventory.stock.read' && can(p)) ?? '/dashboard'} replace />
}
