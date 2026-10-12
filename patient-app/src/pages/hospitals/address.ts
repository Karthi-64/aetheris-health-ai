import type { HospitalAddress } from '@/api/hospitals'

const text = (value: unknown) => (typeof value === 'string' ? value.trim() : '')

/** Where a hospital is, in one line for a card: city and state, else the first address line. */
export function locationLine(address: HospitalAddress | null | undefined): string {
  const place = [text(address?.city), text(address?.state)].filter(Boolean).join(', ')
  return place || text(address?.line1)
}

/** A postal address as the lines to print, leaving out whatever is missing. */
export function addressLines(address: HospitalAddress | null | undefined): string[] {
  const place = [text(address?.city), text(address?.state)].filter(Boolean).join(', ')
  return [
    text(address?.line1),
    text(address?.line2),
    [place, text(address?.postal_code)].filter(Boolean).join(' '),
    text(address?.country),
  ].filter(Boolean)
}

/** A phone number as people write one: digits, spaces and the usual punctuation, on one line. */
const WRITTEN_NUMBER = /^\+?[\d ().-]+$/

/** The fewest digits worth dialling, and the most a number can have (E.164). */
const MIN_DIGITS = 3
const MAX_DIGITS = 15

/**
 * A `tel:` link for a phone number, or `null` when it is not plainly one — an
 * extension, a note, anything with letters is shown as text instead of being
 * dialled wrongly. The link is built from the digits alone (and a leading
 * plus), so it can only ever be a phone call.
 */
export function telHref(phone: string | null | undefined): string | null {
  const shown = text(phone)
  if (!WRITTEN_NUMBER.test(shown)) return null
  const digits = shown.replace(/\D/g, '')
  if (digits.length < MIN_DIGITS || digits.length > MAX_DIGITS) return null
  return `tel:${shown.startsWith('+') ? '+' : ''}${digits}`
}
