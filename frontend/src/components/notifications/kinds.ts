import { Bell, Boxes, FlaskConical, KeyRound, Megaphone, Receipt, UserPlus, type LucideIcon } from 'lucide-react'

interface KindPresentation {
  icon: LucideIcon
  /** The catalog's category for this kind, or null for a kind this build does not know. */
  category: string | null
}

/**
 * Icon and category for each notification kind (docs/18-API_CONTRACTS.md §7.4).
 * The categories are the ones the backend catalog uses on the preferences
 * page, so a notification reads the same in both places.
 */
const KINDS: Record<string, KindPresentation> = {
  'auth.user_invited': { icon: UserPlus, category: 'Account' },
  'auth.password_reset_requested': { icon: KeyRound, category: 'Account' },
  'billing.discount_approval_requested': { icon: Receipt, category: 'Billing' },
  'lab.results_released': { icon: FlaskConical, category: 'Laboratory' },
  'lab.critical_result': { icon: FlaskConical, category: 'Laboratory' },
  'lab.result_amended': { icon: FlaskConical, category: 'Laboratory' },
  'inventory.low_stock': { icon: Boxes, category: 'Inventory' },
  'system.broadcast': { icon: Megaphone, category: 'Announcements' },
}

/** A kind added on the server later still renders: generic icon, no category. */
export function kindPresentation(kind: string): KindPresentation {
  return KINDS[kind] ?? { icon: Bell, category: null }
}

/**
 * Whether a notification's `link` is safe to route to. The API only ever sends
 * an in-app path (§7.2); anything else — an absolute or protocol-relative URL
 * — is not followed.
 */
export function isInAppPath(link: string | null): link is string {
  return !!link && link.startsWith('/') && !link.startsWith('//') && !link.includes('\\')
}
