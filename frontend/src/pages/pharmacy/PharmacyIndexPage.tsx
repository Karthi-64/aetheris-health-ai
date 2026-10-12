import { Navigate } from 'react-router-dom'
import { usePermissions } from '@/hooks/usePermissions'
import { firstPharmacySection } from './pharmacySections'

/**
 * `/pharmacy` itself: sends the user to the first Pharmacy screen their
 * permissions cover — prescriptions for a doctor or pharmacist, the catalog
 * for an inventory manager.
 */
export default function PharmacyIndexPage() {
  const { can } = usePermissions()
  return <Navigate to={firstPharmacySection(can) ?? '/dashboard'} replace />
}
