/**
 * Formatting helpers. Money is rendered with a currency the caller supplies
 * (the hospital's currency, from user context); until that is wired the default
 * is a plain 2-decimal amount with no symbol, so we never imply the wrong one.
 */
export function formatMoney(value: string | number, currency?: string): string {
  const amount = typeof value === 'string' ? Number(value) : value
  if (Number.isNaN(amount)) return '—'
  if (currency) {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(amount)
  }
  return new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(amount)
}

/** Medium date (e.g. "2 May 1971") from an ISO string; echoes the input if unparseable. */
export function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleDateString(undefined, { dateStyle: 'medium' })
}

/** Short local time (e.g. "9:30 AM") from an ISO datetime; echoes the input if unparseable. */
export function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString(undefined, { timeStyle: 'short' })
}

/** Today's date in the viewer's local timezone as YYYY-MM-DD (for date inputs and day filters). */
export function todayISODate(): string {
  const d = new Date()
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}
