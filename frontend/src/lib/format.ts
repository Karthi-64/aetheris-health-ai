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

/** Medium date with a short time (e.g. "Oct 5, 2026, 9:30 AM"); echoes the input if unparseable. */
export function formatDateTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? iso
    : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/** Short local time (e.g. "9:30 AM") from an ISO datetime; echoes the input if unparseable. */
export function formatTime(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString(undefined, { timeStyle: 'short' })
}

/**
 * Short time in a named IANA zone — for times that belong to the hospital's
 * clock (a doctor's slots), which is not necessarily the viewer's. Without a
 * zone, or with one the browser does not know, the viewer's clock is used.
 */
export function formatTimeIn(iso: string, timeZone?: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  try {
    return d.toLocaleTimeString(undefined, { timeStyle: 'short', timeZone })
  } catch {
    return d.toLocaleTimeString(undefined, { timeStyle: 'short' })
  }
}

/** Day with its weekday (e.g. "Tue, 6 Oct 2026") in a named IANA zone; see {@link formatTimeIn}. */
export function formatDayIn(iso: string, timeZone?: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  const style = { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' } as const
  try {
    return d.toLocaleDateString(undefined, { ...style, timeZone })
  } catch {
    return d.toLocaleDateString(undefined, style)
  }
}

/**
 * The calendar day (YYYY-MM-DD) an instant falls on in a named IANA zone — the
 * value a date input and the API's day filters expect. See {@link formatTimeIn}.
 */
export function isoDateIn(iso: string, timeZone?: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const parts = (zone?: string) => {
    const p = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).formatToParts(d)
    const get = (type: string) => p.find((x) => x.type === type)?.value ?? ''
    return `${get('year')}-${get('month')}-${get('day')}`
  }
  try {
    return parts(timeZone)
  } catch {
    return parts()
  }
}

/**
 * How long ago something happened, in the viewer's language: "now",
 * "5 minutes ago", "yesterday". Past a week it is clearer as a date, so it
 * falls back to {@link formatDate}. `now` is injectable for tests.
 */
export function formatRelativeTime(iso: string, now: number = Date.now()): string {
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return iso
  const seconds = Math.round((then - now) / 1000)
  const elapsed = Math.abs(seconds)
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  if (elapsed < 45) return relative.format(0, 'second')
  if (elapsed < 3600) return relative.format(Math.round(seconds / 60), 'minute')
  if (elapsed < 86_400) return relative.format(Math.round(seconds / 3600), 'hour')
  if (elapsed < 7 * 86_400) return relative.format(Math.round(seconds / 86_400), 'day')
  return formatDate(iso)
}

/** Today's date in the viewer's local timezone as YYYY-MM-DD (for date inputs and day filters). */
export function todayISODate(): string {
  const d = new Date()
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 10)
}

/** A whole number with the viewer's digit grouping (e.g. "1,204"). Formatting only. */
export function formatCount(n: number): string {
  return new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(n)
}

/**
 * A short amount for a chart axis or tooltip (e.g. "₹1.9K", "₹4.4M") — never
 * for a figure a user reads off as exact; those use {@link formatMoney}. An
 * unknown currency code falls back to the bare compact number.
 */
export function formatMoneyCompact(value: string | number, currency?: string): string {
  const amount = typeof value === 'string' ? Number(value) : value
  if (Number.isNaN(amount)) return '—'
  const compact = { notation: 'compact', maximumFractionDigits: 1 } as const
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, { ...compact, style: 'currency', currency }).format(amount)
    } catch {
      // Not a currency code Intl knows: show the number without a symbol.
    }
  }
  return new Intl.NumberFormat(undefined, compact).format(amount)
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** The parts of a `YYYY-MM-DD` date, read from the text so no timezone can shift the day. */
function dateParts(date: string): { year: string; month: string; day: number } | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  const month = m ? MONTHS[Number(m[2]) - 1] : undefined
  return m && month ? { year: m[1], month, day: Number(m[3]) } : null
}

/**
 * The label of one report period, from the hospital-local dates the server
 * sent: a day is "6 Oct", a week "5–11 Oct" (or "28 Sep – 4 Oct" across
 * months, with the years when it crosses one) and a month "Oct 2026". Built
 * from the date text, never through a `Date`, so the viewer's timezone cannot
 * move a bucket to the wrong day. Echoes `bucket_start` if it is not a date.
 */
export function formatBucketLabel(
  bucket: { bucket_start: string; bucket_end: string },
  granularity: 'day' | 'week' | 'month',
): string {
  const start = dateParts(bucket.bucket_start)
  if (!start) return bucket.bucket_start
  if (granularity === 'month') return `${start.month} ${start.year}`
  if (granularity === 'day') return `${start.day} ${start.month}`
  const end = dateParts(bucket.bucket_end)
  if (!end) return `${start.day} ${start.month}`
  if (start.year !== end.year) {
    return `${start.day} ${start.month} ${start.year} – ${end.day} ${end.month} ${end.year}`
  }
  if (start.month !== end.month) return `${start.day} ${start.month} – ${end.day} ${end.month}`
  return `${start.day}–${end.day} ${start.month}`
}
