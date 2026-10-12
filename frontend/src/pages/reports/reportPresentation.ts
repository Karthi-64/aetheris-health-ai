import type { AgeingBucketKey, AppointmentCounts, Granularity, PaymentMethod } from '@/api/reports'
import { formatCount } from '@/lib/format'

/**
 * Words for the values the Reports API sends. Formatting only: nothing here
 * adds, subtracts or averages a figure.
 */

export const PAYMENT_METHOD_LABELS: Record<PaymentMethod, string> = {
  cash: 'Cash',
  card: 'Card',
  upi: 'UPI',
  bank_transfer: 'Bank transfer',
  insurance: 'Insurance',
}

export const AGEING_LABELS: Record<AgeingBucketKey, string> = {
  '0_30': '0–30 days',
  '31_60': '31–60 days',
  '61_90': '61–90 days',
  over_90: 'Over 90 days',
}

/** "per day", "per week", "per month" — for a chart's description. */
export const GRANULARITY_NOUNS: Record<Granularity, string> = {
  day: 'day',
  week: 'week',
  month: 'month',
}

/**
 * The most rows a period table can hold: the server caps a period at twelve
 * months, 366 days at most, and the table adds one Total row. The table is
 * given room for all of them so the Total is never on another page.
 */
export const PERIOD_TABLE_PAGE_SIZE = 400

/**
 * The series of the "Appointments by status" chart, in stacking order, with the
 * words the legend and tooltip use. Each carries its own colour: the shared
 * palette has five and would hand the sixth series the first one's colour,
 * drawing No-show exactly like Completed.
 */
export const APPOINTMENT_SERIES: {
  key: Exclude<keyof AppointmentCounts, 'total'>
  label: string
  color: string
}[] = [
  { key: 'completed', label: 'Completed', color: 'var(--color-chart-1)' },
  { key: 'booked', label: 'Booked', color: 'var(--color-chart-2)' },
  { key: 'checked_in', label: 'Checked in', color: 'var(--color-chart-3)' },
  { key: 'in_progress', label: 'In progress', color: 'var(--color-chart-4)' },
  { key: 'cancelled', label: 'Cancelled', color: 'var(--color-outline)' },
  { key: 'no_show', label: 'No-show', color: 'var(--color-error)' },
]

/**
 * A hospital-local calendar date ("2026-10-06") as a medium date. Read as a
 * UTC day and printed in UTC, so the viewer's timezone cannot move it.
 * Echoes a value that is not a date.
 */
export function plainDate(date: string): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!m) return date
  return new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]))).toLocaleDateString(undefined, {
    dateStyle: 'medium',
    timeZone: 'UTC',
  })
}

/** The period a report covers, as the server resolved it. */
export function periodText(range: { from: string; to: string }): string {
  return `${plainDate(range.from)} to ${plainDate(range.to)}`
}

/** A server count with its noun: "1 invoice", "1,204 invoices". */
export function countOf(n: number, singular: string, plural: string): string {
  return `${formatCount(n)} ${n === 1 ? singular : plural}`
}
