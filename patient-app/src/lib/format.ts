/** English only in V1 (spec 25.6); day-month-year so a date cannot be misread. */
const LOCALE = 'en-GB'

/** True when `value` is a real calendar date written `YYYY-MM-DD`. */
export function isIsoDate(value: string): boolean {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return false
  const [year, month, day] = [Number(match[1]), Number(match[2]), Number(match[3])]
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year && date.getUTCMonth() === month - 1 && date.getUTCDate() === day
}

/**
 * An instant written as ISO-8601 with its UTC offset (`Z` or `±hh:mm`), e.g.
 * `2026-10-07T11:30:00+05:30`. A date alone, a time without an offset, or any
 * other spelling `Date.parse` would accept is not one: an instant that does
 * not say what clock it is on cannot be shown on any other.
 */
const ISO_INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:\d{2})$/

/** The epoch milliseconds of an ISO instant with an offset, or `null` for anything else. */
export function parseInstant(value: string): number | null {
  if (!ISO_INSTANT.test(value)) return null
  const ms = Date.parse(value)
  return Number.isNaN(ms) ? null : ms
}

/** A calendar date as a UTC instant at noon: safe from any zone's day boundary. */
const noonOf = (date: string) =>
  Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10)), 12)

const DAY_MS = 86_400_000

/** The calendar date `n` days after `date` (negative for before). */
export function addDays(date: string, n: number): string {
  return new Date(noonOf(date) + n * DAY_MS).toISOString().slice(0, 10)
}

/** Whole days from `from` to `to`; negative when `to` is earlier. */
export function daysBetween(from: string, to: string): number {
  return Math.round((noonOf(to) - noonOf(from)) / DAY_MS)
}

/** Today in the viewer's own calendar, as `YYYY-MM-DD`. */
export function todayIsoDate(): string {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
}

/**
 * A calendar date (`YYYY-MM-DD`, no time, no zone) written out in full, e.g.
 * "17 May 1990". Formatted in UTC so the day never shifts with the viewer's
 * zone; echoes the input if it is not a date.
 */
export function formatCalendarDate(iso: string): string {
  if (!isIsoDate(iso)) return iso
  return new Date(`${iso}T00:00:00Z`).toLocaleDateString(LOCALE, {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    timeZone: 'UTC',
  })
}

/** The day of an ISO instant in the viewer's zone, e.g. "3 Oct 2026"; echoes the input if unparseable. */
export function formatDay(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleDateString(LOCALE, { day: 'numeric', month: 'short', year: 'numeric' })
}
