import { addDays, daysBetween, isIsoDate, parseInstant } from '@/lib/format'

/**
 * Dates and times of a doctor's availability. Every time here is the
 * hospital's: a calendar date (`YYYY-MM-DD`) is one of the hospital's local
 * days, and an instant is shown on the hospital's clock, never the browser's.
 * Nothing in this file reads the browser's clock or zone: "today" is whatever
 * the server said it is.
 */

/** English only in V1 (spec 25.6); day-month-year so a date cannot be misread. */
const LOCALE = 'en-GB'

/** How many days one window of the day strip covers. */
export const WINDOW_DAYS = 7

/** A calendar date (`YYYY-MM-DD`) that exists. */
export const isCalendarDate = isIsoDate

export { addDays, daysBetween, parseInstant }

/** A calendar date as a UTC instant at noon: safe from any zone's day boundary. */
const noonOf = (date: string) =>
  Date.UTC(Number(date.slice(0, 4)), Number(date.slice(5, 7)) - 1, Number(date.slice(8, 10)), 12)

/** Calendar dates compare as text: `YYYY-MM-DD` sorts as it reads. */
const latest = (a: string, b: string) => (a > b ? a : b)
const earliest = (a: string, b: string) => (a < b ? a : b)

/** True when `date` is a day the patient may be shown: from today to the end of the horizon, inclusive. */
export function isSelectableDate(date: string, today: string, horizonEnd: string): boolean {
  return isCalendarDate(date) && date >= today && date <= horizonEnd
}

export interface DateWindow {
  start: string
  end: string
}

/** `window` pulled inside `[today, horizonEnd]`. A horizon before today is today. */
export function clampWindow(window: DateWindow, today: string, horizonEnd: string): DateWindow {
  const last = latest(horizonEnd, today)
  const start = earliest(latest(window.start, today), last)
  const end = earliest(latest(window.end, start), last)
  return { start, end }
}

/**
 * The window that holds `date`. Windows are laid out from today in steps of
 * {@link WINDOW_DAYS}, so the one holding a date is the same whichever way it
 * was reached — by "Later days", by a link, by a reload — and the last one is
 * cut short at the horizon. A date outside the horizon gets the first window.
 */
export function windowContaining(date: string, today: string, horizonEnd: string): DateWindow {
  const step = isSelectableDate(date, today, horizonEnd) ? Math.floor(daysBetween(today, date) / WINDOW_DAYS) : 0
  const start = addDays(today, step * WINDOW_DAYS)
  return clampWindow({ start, end: addDays(start, WINDOW_DAYS - 1) }, today, horizonEnd)
}

/** True when the browser knows `timeZone` as an IANA zone it can format in. */
export function isKnownTimeZone(timeZone: string): boolean {
  try {
    new Intl.DateTimeFormat(LOCALE, { timeZone })
    return true
  } catch {
    return false
  }
}

const partsOf = (formatter: Intl.DateTimeFormat, ms: number): Record<string, string> =>
  Object.fromEntries(formatter.formatToParts(ms).map((part) => [part.type, part.value]))

/**
 * The wall-clock time of an instant on the hospital's clock, e.g. `11:30`.
 * When the browser does not know the zone, the time is read off the instant's
 * own offset — the server writes every slot with the hospital's — so the
 * clock shown is still the hospital's, never the browser's. Not an instant: empty.
 */
export function formatSlotTime(iso: string, timeZone: string): string {
  const ms = parseInstant(iso)
  if (ms === null) return ''
  if (isKnownTimeZone(timeZone)) {
    const { hour, minute } = partsOf(
      new Intl.DateTimeFormat(LOCALE, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone }),
      ms,
    )
    return `${hour}:${minute}`
  }
  return iso.slice(11, 16)
}

/** The hospital's calendar date of an instant, e.g. `2026-10-07`; `null` when either cannot be read. */
export function localDateOf(iso: string, timeZone: string): string | null {
  const ms = parseInstant(iso)
  if (ms === null || !isKnownTimeZone(timeZone)) return null
  const { year, month, day } = partsOf(
    new Intl.DateTimeFormat(LOCALE, { year: 'numeric', month: '2-digit', day: '2-digit', timeZone }),
    ms,
  )
  return `${year}-${month}-${day}`
}

export interface DayParts {
  weekday: string
  day: string
  month: string
  year: string
}

/**
 * The parts of a calendar date, written out: "Wednesday", "7", "October",
 * "2026". A calendar date names one of the hospital's days already, so it is
 * formatted in UTC — no zone, the browser's least of all, can move it to
 * another day. Not a date: every part empty.
 */
export function dayParts(date: string): DayParts {
  if (!isCalendarDate(date)) return { weekday: '', day: '', month: '', year: '' }
  const parts = partsOf(
    new Intl.DateTimeFormat(LOCALE, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric', timeZone: 'UTC' }),
    noonOf(date),
  )
  return { weekday: parts.weekday ?? '', day: parts.day ?? '', month: parts.month ?? '', year: parts.year ?? '' }
}

/** One chosen slot, as a booking link carries it. */
export interface ChosenSlot {
  date: string
  start: string
  end: string
}

/**
 * The slot a booking link names, if the link is sound: a real date, two
 * instants with offsets in order, and the start falling on that date by the
 * hospital's clock. Anything less is no slot — the link is not acted on, and
 * nothing of it is shown.
 */
export function readChosenSlot(params: URLSearchParams, timeZone: string): ChosenSlot | null {
  const date = params.get('date') ?? ''
  const start = params.get('start') ?? ''
  const end = params.get('end') ?? ''
  if (!isCalendarDate(date)) return null
  const startMs = parseInstant(start)
  const endMs = parseInstant(end)
  if (startMs === null || endMs === null || endMs <= startMs) return null
  if (localDateOf(start, timeZone) !== date) return null
  return { date, start, end }
}

/** A calendar date in full, e.g. "Wednesday 7 October 2026"; `short` is "Wed 7 Oct". Not a date: empty. */
export function formatDay(date: string, style: 'long' | 'short' = 'long'): string {
  if (!isCalendarDate(date)) return ''
  const { weekday, day, month, year } = dayParts(date)
  return style === 'long' ? `${weekday} ${day} ${month} ${year}` : `${weekday.slice(0, 3)} ${day} ${month.slice(0, 3)}`
}
