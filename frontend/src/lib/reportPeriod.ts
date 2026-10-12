/**
 * Calendar-date helpers for the report filters. Pure string and UTC-date
 * arithmetic on `YYYY-MM-DD` values: no browser timezone is ever involved, and
 * none of them reads the clock — "today" is always the `meta.today` the server
 * sent (the hospital's day), passed in by the caller.
 *
 * The two period rules mirror the server's (`backend/app/services/report_periods.py`)
 * so a range it would refuse is caught before a request is made; the server
 * remains the judge.
 */

export type ReportGranularity = 'day' | 'week' | 'month'

export const GRANULARITIES: readonly ReportGranularity[] = ['day', 'week', 'month']

export const GRANULARITY_LABELS: Record<ReportGranularity, string> = {
  day: 'Daily',
  week: 'Weekly',
  month: 'Monthly',
}

export function isGranularity(value: unknown): value is ReportGranularity {
  return typeof value === 'string' && (GRANULARITIES as readonly string[]).includes(value)
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

const pad = (n: number, width = 2) => String(n).padStart(width, '0')

const toISO = (d: Date) => `${pad(d.getUTCFullYear(), 4)}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`

function parts(date: string): { year: number; month: number; day: number } | null {
  const m = ISO_DATE.exec(date)
  if (!m) return null
  const [year, month, day] = [Number(m[1]), Number(m[2]), Number(m[3])]
  const d = new Date(Date.UTC(year, month - 1, day))
  // A day the month does not have (2026-02-30) rolls over; that is not a date.
  if (d.getUTCMonth() !== month - 1 || d.getUTCDate() !== day) return null
  return { year, month, day }
}

/** True for a real calendar date written `YYYY-MM-DD`. */
export function isISODate(value: unknown): value is string {
  return typeof value === 'string' && parts(value) !== null
}

/** True for a UUID, the form of a `doctor_id` or `department_id`. */
export function isUuid(value: unknown): value is string {
  return typeof value === 'string' && UUID.test(value)
}

/** `date` moved by `days` (negative goes back). Echoes a value that is not a date. */
export function addDays(date: string, days: number): string {
  const p = parts(date)
  if (!p) return date
  return toISO(new Date(Date.UTC(p.year, p.month - 1, p.day + days)))
}

/**
 * `date` moved by whole calendar `months`, the day clamped to the target
 * month's last day (31 Jan + 1 month is 28 or 29 Feb) — the server's
 * `add_months`. Echoes a value that is not a date.
 */
export function addMonths(date: string, months: number): string {
  const p = parts(date)
  if (!p) return date
  const first = new Date(Date.UTC(p.year, p.month - 1 + months, 1))
  const lastDay = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 0)).getUTCDate()
  return toISO(new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), Math.min(p.day, lastDay))))
}

/** The 1st of `date`'s month. */
export function startOfMonth(date: string): string {
  return parts(date) ? `${date.slice(0, 8)}01` : date
}

/** The server's wording for a range that ends before it starts. */
export const PERIOD_ORDER_MESSAGE = '`to` must not be before `from`.'
/** The server's wording for a range longer than 12 calendar months. */
export const PERIOD_LENGTH_MESSAGE = 'Date range must not exceed 12 months.'

/**
 * Why the server would refuse this period, in its own words, or null when it
 * would not. Only a range with both ends can be judged here: with one end
 * missing the other is the server's default, which depends on its "today".
 */
export function periodError(from: string | undefined, to: string | undefined): string | null {
  if (!from || !to || !isISODate(from) || !isISODate(to)) return null
  if (to < from) return PERIOD_ORDER_MESSAGE
  if (to >= addMonths(from, 12)) return PERIOD_LENGTH_MESSAGE
  return null
}

export type PeriodPreset = 'last_7_days' | 'last_30_days' | 'this_month' | 'last_12_months'

export const PERIOD_PRESETS: readonly { id: PeriodPreset; label: string }[] = [
  { id: 'last_7_days', label: 'Last 7 days' },
  { id: 'last_30_days', label: 'Last 30 days' },
  { id: 'this_month', label: 'This month' },
  { id: 'last_12_months', label: 'Last 12 months' },
]

/**
 * The range a preset stands for, ending on `today` (the hospital's day, from
 * `meta.today`).
 *
 * "Last 12 months" adds the day first and then goes back twelve months. The
 * other order gives 2024-02-29..2025-02-28 on 28 Feb 2025, one day more than
 * the server allows; this order gives 2024-03-01..2025-02-28.
 */
export function presetRange(preset: PeriodPreset, today: string): { from: string; to: string } {
  switch (preset) {
    case 'last_7_days':
      return { from: addDays(today, -6), to: today }
    case 'last_30_days':
      return { from: addDays(today, -29), to: today }
    case 'this_month':
      return { from: startOfMonth(today), to: today }
    case 'last_12_months':
      return { from: addMonths(addDays(today, 1), -12), to: today }
  }
}
