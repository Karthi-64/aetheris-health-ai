import type { Permission } from '@/lib/rbac'

/** The Pharmacy screens, each with the permission its list endpoint requires (§9.1). */
export const PHARMACY_SECTIONS: { to: string; label: string; permission: Permission }[] = [
  { to: '/pharmacy/prescriptions', label: 'Prescriptions', permission: 'pharmacy.prescription.read' },
  { to: '/pharmacy/medicines', label: 'Medicines', permission: 'pharmacy.medicine.read' },
  { to: '/pharmacy/purchase-orders', label: 'Purchase orders', permission: 'pharmacy.po.read' },
  { to: '/pharmacy/vendors', label: 'Vendors', permission: 'pharmacy.vendor.read' },
]

/** The first Pharmacy screen this user may open, or undefined if there is none. */
export function firstPharmacySection(can: (permission: Permission) => boolean): string | undefined {
  return PHARMACY_SECTIONS.find((s) => can(s.permission))?.to
}
