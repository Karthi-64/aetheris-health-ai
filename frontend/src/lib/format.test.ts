import { describe, it, expect } from 'vitest'
import {
  formatBucketLabel,
  formatCount,
  formatDate,
  formatDayIn,
  formatMoneyCompact,
  formatRelativeTime,
  formatTimeIn,
  isoDateIn,
} from './format'

describe('formatRelativeTime', () => {
  const now = Date.parse('2026-10-05T12:00:00Z')
  const ago = (ms: number) => new Date(now - ms).toISOString()
  // Compared with Intl's own output so the test holds in any locale.
  const relative = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  const MINUTE = 60_000
  const HOUR = 60 * MINUTE
  const DAY = 24 * HOUR

  it('treats the last few seconds as now', () => {
    expect(formatRelativeTime(ago(10_000), now)).toBe(relative.format(0, 'second'))
  })

  it('counts minutes, then hours, then days', () => {
    expect(formatRelativeTime(ago(5 * MINUTE), now)).toBe(relative.format(-5, 'minute'))
    expect(formatRelativeTime(ago(3 * HOUR), now)).toBe(relative.format(-3, 'hour'))
    expect(formatRelativeTime(ago(DAY), now)).toBe(relative.format(-1, 'day'))
    expect(formatRelativeTime(ago(6 * DAY), now)).toBe(relative.format(-6, 'day'))
  })

  it('falls back to a date once it is more than a week old', () => {
    const iso = ago(30 * DAY)
    expect(formatRelativeTime(iso, now)).toBe(formatDate(iso))
  })

  it('echoes a value it cannot parse', () => {
    expect(formatRelativeTime('not a date', now)).toBe('not a date')
  })
})

describe('hospital-clock helpers', () => {
  // 20:00 UTC on 6 Oct is already 01:30 on 7 Oct in Asia/Kolkata.
  const lateEvening = '2026-10-06T20:00:00Z'

  it('isoDateIn gives the calendar day in the named zone', () => {
    expect(isoDateIn(lateEvening, 'Asia/Kolkata')).toBe('2026-10-07')
    expect(isoDateIn(lateEvening, 'UTC')).toBe('2026-10-06')
    expect(isoDateIn(lateEvening, 'America/Los_Angeles')).toBe('2026-10-06')
  })

  it('isoDateIn falls back to the viewer for an unknown zone, and is empty for a bad date', () => {
    expect(isoDateIn(lateEvening, 'Not/AZone')).toMatch(/^2026-10-0[67]$/)
    expect(isoDateIn('not a date', 'UTC')).toBe('')
  })

  it('formatDayIn names the day in the named zone', () => {
    expect(formatDayIn(lateEvening, 'Asia/Kolkata')).toMatch(/Wed.*7/)
    expect(formatDayIn(lateEvening, 'UTC')).toMatch(/Tue.*6/)
    expect(formatDayIn('not a date', 'UTC')).toBe('not a date')
  })

  it('formatTimeIn uses the named zone, or the viewer without one', () => {
    expect(formatTimeIn(lateEvening, 'Asia/Kolkata')).toMatch(/1:30/)
    expect(formatTimeIn(lateEvening)).toBe(formatTimeIn(lateEvening, undefined))
  })
})

describe('formatBucketLabel', () => {
  const bucket = (bucket_start: string, bucket_end: string) => ({ bucket_start, bucket_end, partial: false })

  it('labels a day by its date', () => {
    expect(formatBucketLabel(bucket('2026-10-06', '2026-10-06'), 'day')).toBe('6 Oct')
    expect(formatBucketLabel(bucket('2026-01-31', '2026-01-31'), 'day')).toBe('31 Jan')
  })

  it('labels a week by its Monday and Sunday', () => {
    expect(formatBucketLabel(bucket('2026-10-05', '2026-10-11'), 'week')).toBe('5–11 Oct')
  })

  it('names both months, and both years, when a week crosses them', () => {
    expect(formatBucketLabel(bucket('2026-09-28', '2026-10-04'), 'week')).toBe('28 Sep – 4 Oct')
    expect(formatBucketLabel(bucket('2025-12-29', '2026-01-04'), 'week')).toBe('29 Dec 2025 – 4 Jan 2026')
  })

  it('labels a month by its name and year', () => {
    expect(formatBucketLabel(bucket('2026-10-01', '2026-10-31'), 'month')).toBe('Oct 2026')
    expect(formatBucketLabel(bucket('2025-12-01', '2025-12-31'), 'month')).toBe('Dec 2025')
  })

  it('reads the date text, so the first of a month is never shown as the day before', () => {
    // Through a Date, midnight UTC on the 1st is 30 Sep anywhere west of Greenwich.
    expect(formatBucketLabel(bucket('2026-10-01', '2026-10-01'), 'day')).toBe('1 Oct')
  })

  it('echoes a start it cannot read', () => {
    expect(formatBucketLabel(bucket('not-a-date', '2026-10-06'), 'day')).toBe('not-a-date')
    expect(formatBucketLabel(bucket('2026-13-01', '2026-13-31'), 'month')).toBe('2026-13-01')
  })
})

describe('formatCount and formatMoneyCompact', () => {
  it('formatCount groups digits the way Intl does and adds nothing', () => {
    expect(formatCount(0)).toBe('0')
    expect(formatCount(1204)).toBe(new Intl.NumberFormat().format(1204))
  })

  it('formatMoneyCompact shortens an amount in the given currency', () => {
    const expected = new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 1,
      style: 'currency',
      currency: 'INR',
    }).format(4350)
    expect(formatMoneyCompact('4350.00', 'INR')).toBe(expected)
    expect(formatMoneyCompact(4350, 'INR')).toBe(expected)
  })

  it('formatMoneyCompact keeps the sign of a negative net', () => {
    expect(formatMoneyCompact('-350.00', 'INR')).toMatch(/^[-−]/)
  })

  it('formatMoneyCompact survives an unknown currency and a non-number', () => {
    expect(formatMoneyCompact('1200.00', 'NOT-A-CODE')).toBe(
      new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 }).format(1200),
    )
    expect(formatMoneyCompact('n/a', 'INR')).toBe('—')
  })
})
