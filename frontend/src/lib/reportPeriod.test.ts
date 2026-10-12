import { describe, it, expect } from 'vitest'
import {
  addDays,
  addMonths,
  isGranularity,
  isISODate,
  isUuid,
  PERIOD_LENGTH_MESSAGE,
  PERIOD_ORDER_MESSAGE,
  PERIOD_PRESETS,
  periodError,
  presetRange,
  startOfMonth,
} from './reportPeriod'

describe('isISODate', () => {
  it('accepts a real calendar date', () => {
    expect(isISODate('2026-10-06')).toBe(true)
    expect(isISODate('2024-02-29')).toBe(true)
  })

  it('refuses anything else', () => {
    expect(isISODate('2026-02-30')).toBe(false)
    expect(isISODate('2025-02-29')).toBe(false)
    expect(isISODate('2026-13-01')).toBe(false)
    expect(isISODate('06/10/2026')).toBe(false)
    expect(isISODate('2026-10-06T00:00:00Z')).toBe(false)
    expect(isISODate('')).toBe(false)
    expect(isISODate(undefined)).toBe(false)
    expect(isISODate(20261006)).toBe(false)
  })
})

describe('isGranularity and isUuid', () => {
  it('knows the three granularities, in lower case only', () => {
    expect(['day', 'week', 'month'].every(isGranularity)).toBe(true)
    expect(isGranularity('Day')).toBe(false)
    expect(isGranularity('hour')).toBe(false)
    expect(isGranularity(null)).toBe(false)
  })

  it('recognises a UUID', () => {
    expect(isUuid('5b0c1c7e-2f0a-4d0e-9c53-0a1d6f3b9a11')).toBe(true)
    expect(isUuid('5B0C1C7E-2F0A-4D0E-9C53-0A1D6F3B9A11')).toBe(true)
    expect(isUuid('5b0c1c7e')).toBe(false)
    expect(isUuid("1' OR '1'='1")).toBe(false)
    expect(isUuid(undefined)).toBe(false)
  })
})

describe('addDays', () => {
  it('crosses months and years', () => {
    expect(addDays('2026-10-06', -6)).toBe('2026-09-30')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2024-02-28', 1)).toBe('2024-02-29')
    expect(addDays('2025-02-28', 1)).toBe('2025-03-01')
  })

  it('does not depend on a daylight-saving change', () => {
    // Clocks change in many zones on these days; a calendar day is still one day.
    expect(addDays('2026-03-08', 1)).toBe('2026-03-09')
    expect(addDays('2026-11-01', 1)).toBe('2026-11-02')
  })

  it('echoes a value that is not a date', () => {
    expect(addDays('soon', 1)).toBe('soon')
  })
})

describe('addMonths', () => {
  it('keeps the day when the target month has it', () => {
    expect(addMonths('2026-01-15', 1)).toBe('2026-02-15')
    expect(addMonths('2026-01-01', 12)).toBe('2027-01-01')
    expect(addMonths('2025-03-01', -12)).toBe('2024-03-01')
  })

  it('clamps to the last day of a shorter month', () => {
    expect(addMonths('2026-01-31', 1)).toBe('2026-02-28')
    expect(addMonths('2024-01-31', 1)).toBe('2024-02-29')
    expect(addMonths('2024-02-29', 12)).toBe('2025-02-28')
    expect(addMonths('2026-03-31', -1)).toBe('2026-02-28')
    expect(addMonths('2026-10-31', 1)).toBe('2026-11-30')
  })
})

describe('startOfMonth', () => {
  it('is the 1st of the same month', () => {
    expect(startOfMonth('2026-10-06')).toBe('2026-10-01')
    expect(startOfMonth('2026-10-01')).toBe('2026-10-01')
  })
})

describe('periodError', () => {
  it('passes a single day and an ordinary range', () => {
    expect(periodError('2026-10-06', '2026-10-06')).toBeNull()
    expect(periodError('2026-09-07', '2026-10-06')).toBeNull()
  })

  it('refuses a range that ends before it starts, in the server\'s words', () => {
    expect(periodError('2026-10-06', '2026-10-05')).toBe(PERIOD_ORDER_MESSAGE)
    expect(PERIOD_ORDER_MESSAGE).toBe('`to` must not be before `from`.')
  })

  it('allows twelve calendar months inclusive and refuses one day more', () => {
    expect(periodError('2026-01-01', '2026-12-31')).toBeNull()
    expect(periodError('2026-01-01', '2027-01-01')).toBe(PERIOD_LENGTH_MESSAGE)
    expect(PERIOD_LENGTH_MESSAGE).toBe('Date range must not exceed 12 months.')
  })

  it('clamps the twelve-month limit on a leap day as the server does', () => {
    expect(periodError('2024-02-29', '2025-02-27')).toBeNull()
    expect(periodError('2024-02-29', '2025-02-28')).toBe(PERIOD_LENGTH_MESSAGE)
  })

  it('does not judge a range with an end missing or malformed', () => {
    expect(periodError('2026-10-06', undefined)).toBeNull()
    expect(periodError(undefined, '2026-10-06')).toBeNull()
    expect(periodError(undefined, undefined)).toBeNull()
    expect(periodError('garbage', '2026-10-06')).toBeNull()
  })
})

describe('presetRange', () => {
  it('lists the four presets in order', () => {
    expect(PERIOD_PRESETS.map((p) => p.label)).toEqual([
      'Last 7 days',
      'Last 30 days',
      'This month',
      'Last 12 months',
    ])
  })

  it('ends every preset on the hospital\'s today', () => {
    const today = '2026-10-06'
    expect(presetRange('last_7_days', today)).toEqual({ from: '2026-09-30', to: today })
    // The same thirty days the server defaults to.
    expect(presetRange('last_30_days', today)).toEqual({ from: '2026-09-07', to: today })
    expect(presetRange('this_month', today)).toEqual({ from: '2026-10-01', to: today })
    expect(presetRange('last_12_months', today)).toEqual({ from: '2025-10-07', to: today })
  })

  it('gives a twelve-month range the server accepts on the last day of February', () => {
    expect(presetRange('last_12_months', '2025-02-28')).toEqual({ from: '2024-03-01', to: '2025-02-28' })
    expect(presetRange('last_12_months', '2024-02-29')).toEqual({ from: '2023-03-01', to: '2024-02-29' })
  })

  it('never produces a range the period rules refuse', () => {
    for (const today of ['2026-10-06', '2025-02-28', '2024-02-29', '2026-12-31', '2026-01-01', '2026-03-31']) {
      for (const { id } of PERIOD_PRESETS) {
        const { from, to } = presetRange(id, today)
        expect(periodError(from, to), `${id} on ${today}`).toBeNull()
      }
    }
  })
})
