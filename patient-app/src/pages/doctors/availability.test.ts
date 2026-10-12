import { describe, expect, it } from 'vitest'
import { isSendableRange, toAvailability } from '@/api/availability'
import {
  addDays,
  clampWindow,
  dayParts,
  daysBetween,
  formatDay,
  formatSlotTime,
  isKnownTimeZone,
  isSelectableDate,
  localDateOf,
  parseInstant,
  readChosenSlot,
  windowContaining,
} from '@/pages/doctors/availability'
import { doctorAvailabilityPath, doctorBookingPath } from '@/pages/doctors/paths'
import { availabilityOf, instantOf, slotsOf } from '@/test/availability'

const REF = 'd0000000-0000-4000-8000-000000000001'
const TODAY = '2026-10-07'
const HORIZON = '2026-11-06'

describe('calendar dates', () => {
  it('adds and subtracts whole days, across a month end and a year end', () => {
    expect(addDays(TODAY, 1)).toBe('2026-10-08')
    expect(addDays(TODAY, -7)).toBe('2026-09-30')
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01')
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01')
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29')
    expect(daysBetween(TODAY, HORIZON)).toBe(30)
    expect(daysBetween(HORIZON, TODAY)).toBe(-30)
    expect(daysBetween(TODAY, TODAY)).toBe(0)
  })

  it('takes a date for selectable only from today to the horizon, inclusive', () => {
    expect(isSelectableDate(TODAY, TODAY, HORIZON)).toBe(true)
    expect(isSelectableDate(HORIZON, TODAY, HORIZON)).toBe(true)
    expect(isSelectableDate('2026-10-20', TODAY, HORIZON)).toBe(true)
    expect(isSelectableDate('2026-10-06', TODAY, HORIZON)).toBe(false)
    expect(isSelectableDate('2026-11-07', TODAY, HORIZON)).toBe(false)
    for (const junk of ['', 'today', '2026-13-01', '2026-02-30', '2026-10-7', '2026-10-07T00:00', ' 2026-10-08', '20261008']) {
      expect(isSelectableDate(junk, TODAY, HORIZON)).toBe(false)
    }
  })

  it('clamps a window into the bookable days', () => {
    expect(clampWindow({ start: '2026-09-30', end: '2026-10-06' }, TODAY, HORIZON)).toEqual({ start: TODAY, end: TODAY })
    expect(clampWindow({ start: '2026-11-04', end: '2026-11-10' }, TODAY, HORIZON)).toEqual({ start: '2026-11-04', end: HORIZON })
    expect(clampWindow({ start: '2026-10-14', end: '2026-10-20' }, TODAY, HORIZON)).toEqual({ start: '2026-10-14', end: '2026-10-20' })
    // A horizon before today is today: one day, never an empty or backwards range.
    expect(clampWindow({ start: '2026-10-14', end: '2026-10-20' }, TODAY, '2026-10-01')).toEqual({ start: TODAY, end: TODAY })
  })

  it('lays the windows out from today in weeks, the last one cut at the horizon, the same whichever day names them', () => {
    expect(windowContaining(TODAY, TODAY, HORIZON)).toEqual({ start: TODAY, end: '2026-10-13' })
    expect(windowContaining('2026-10-13', TODAY, HORIZON)).toEqual({ start: TODAY, end: '2026-10-13' })
    expect(windowContaining('2026-10-14', TODAY, HORIZON)).toEqual({ start: '2026-10-14', end: '2026-10-20' })
    expect(windowContaining('2026-10-22', TODAY, HORIZON)).toEqual({ start: '2026-10-21', end: '2026-10-27' })
    expect(windowContaining('2026-11-05', TODAY, HORIZON)).toEqual({ start: '2026-11-04', end: HORIZON })
    expect(windowContaining(HORIZON, TODAY, HORIZON)).toEqual({ start: '2026-11-04', end: HORIZON })
    // Outside the bookable days: the first week.
    expect(windowContaining('2026-10-01', TODAY, HORIZON)).toEqual({ start: TODAY, end: '2026-10-13' })
    expect(windowContaining('2027-01-01', TODAY, HORIZON)).toEqual({ start: TODAY, end: '2026-10-13' })
    expect(windowContaining('nope', TODAY, HORIZON)).toEqual({ start: TODAY, end: '2026-10-13' })
  })

  it('writes a date out in full, from the date alone, whatever zone the browser is in', () => {
    expect(formatDay(TODAY)).toBe('Wednesday 7 October 2026')
    expect(formatDay(TODAY, 'short')).toBe('Wed 7 Oct')
    expect(formatDay('2026-11-01')).toBe('Sunday 1 November 2026')
    expect(dayParts('2026-10-08')).toEqual({ weekday: 'Thursday', day: '8', month: 'October', year: '2026' })
    expect(formatDay('2026-02-30')).toBe('')
    expect(formatDay('<b>2026-10-07</b>')).toBe('')
  })
})

describe('instants', () => {
  it.each([
    ['2026-10-07T11:30:00+05:30', Date.UTC(2026, 9, 7, 6, 0)],
    ['2026-10-07T11:30+05:30', Date.UTC(2026, 9, 7, 6, 0)],
    ['2026-10-07T11:30:00.250Z', Date.UTC(2026, 9, 7, 11, 30, 0, 250)],
    ['2026-10-08T09:30:00-04:00', Date.UTC(2026, 9, 8, 13, 30)],
  ])('reads %s as an instant', (value, ms) => {
    expect(parseInstant(value)).toBe(ms)
  })

  it.each([
    '',
    '2026-10-07',
    '2026-10-07T11:30:00',
    '2026-10-07 11:30:00+05:30',
    'Oct 7 2026 11:30 GMT+0530',
    '1791345600000',
    '2026-10-07T11:30:00+0530',
    '2026-13-07T11:30:00+05:30',
    ' 2026-10-07T11:30:00+05:30',
    '2026-10-07T11:30:00+05:30;DROP',
    '<script>2026-10-07T11:30:00Z</script>',
  ])('takes %j for no instant at all', (value) => {
    expect(parseInstant(value)).toBeNull()
  })

  it('shows an instant on the hospital’s clock, whatever offset it was written with and whatever zone the browser is in', () => {
    // The same instant, three spellings: the hospital's clock says 09:30 for each.
    expect(formatSlotTime('2026-10-08T09:30:00+14:00', 'Pacific/Kiritimati')).toBe('09:30')
    expect(formatSlotTime('2026-10-07T19:30:00Z', 'Pacific/Kiritimati')).toBe('09:30')
    expect(formatSlotTime('2026-10-08T01:00:00+05:30', 'Pacific/Kiritimati')).toBe('09:30')
    expect(formatSlotTime('2026-10-07T11:30:00+05:30', 'Asia/Kolkata')).toBe('11:30')
    // Across a daylight-saving change, the wall clock is the zone's on that day.
    expect(formatSlotTime('2026-10-30T13:30:00Z', 'America/New_York')).toBe('09:30')
    expect(formatSlotTime('2026-11-02T14:30:00Z', 'America/New_York')).toBe('09:30')
    // Midnight is 00, not 24.
    expect(formatSlotTime('2026-10-07T18:30:00Z', 'Asia/Kolkata')).toBe('00:00')
    expect(formatSlotTime('not a time', 'Asia/Kolkata')).toBe('')
  })

  it('falls back to the offset the server wrote when the browser does not know the zone — never to the browser’s own', () => {
    expect(isKnownTimeZone('Asia/Kolkata')).toBe(true)
    expect(isKnownTimeZone('<img src=x onerror=window.pwned=1>')).toBe(false)
    expect(isKnownTimeZone('')).toBe(false)
    expect(formatSlotTime('2026-10-07T11:30:00+05:30', '<img src=x onerror=window.pwned=1>')).toBe('11:30')
    expect(formatSlotTime('2026-10-08T09:30:00+14:00', 'Mars/Olympus')).toBe('09:30')
  })

  it('finds the hospital’s date of an instant', () => {
    expect(localDateOf('2026-10-07T19:30:00Z', 'Pacific/Kiritimati')).toBe('2026-10-08')
    expect(localDateOf('2026-10-07T19:30:00Z', 'Asia/Kolkata')).toBe('2026-10-08')
    expect(localDateOf('2026-10-07T19:30:00Z', 'America/New_York')).toBe('2026-10-07')
    expect(localDateOf('2026-10-07T19:30:00Z', 'Mars/Olympus')).toBeNull()
    expect(localDateOf('2026-10-07', 'Asia/Kolkata')).toBeNull()
  })
})

describe('a booking link', () => {
  const read = (query: string, timeZone = 'Asia/Kolkata') => readChosenSlot(new URLSearchParams(query), timeZone)
  const START = '2026-10-08T09:15:00+05:30'
  const END = '2026-10-08T09:30:00+05:30'
  const sound = `date=2026-10-08&start=${encodeURIComponent(START)}&end=${encodeURIComponent(END)}`

  it('names a slot when every part of it holds together', () => {
    expect(read(sound)).toEqual({ date: '2026-10-08', start: START, end: END })
    // The date is the hospital's: 20:00Z on the 8th is the 9th in Kolkata.
    expect(read('date=2026-10-09&start=2026-10-08T20:00:00Z&end=2026-10-08T20:15:00Z')).toEqual({
      date: '2026-10-09',
      start: '2026-10-08T20:00:00Z',
      end: '2026-10-08T20:15:00Z',
    })
  })

  it.each([
    ['no date', `start=${encodeURIComponent(START)}&end=${encodeURIComponent(END)}`],
    ['a date that is not one', `date=2026-02-30&start=${encodeURIComponent(START)}&end=${encodeURIComponent(END)}`],
    ['no start', `date=2026-10-08&end=${encodeURIComponent(END)}`],
    ['a start without an offset', `date=2026-10-08&start=2026-10-08T09:15:00&end=${encodeURIComponent(END)}`],
    ['no end', `date=2026-10-08&start=${encodeURIComponent(START)}`],
    ['an end before the start', `date=2026-10-08&start=${encodeURIComponent(END)}&end=${encodeURIComponent(START)}`],
    ['an end at the start', `date=2026-10-08&start=${encodeURIComponent(START)}&end=${encodeURIComponent(START)}`],
    ['a start on another day', `date=2026-10-09&start=${encodeURIComponent(START)}&end=${encodeURIComponent(END)}`],
    ['a start on another day by the hospital’s clock', 'date=2026-10-08&start=2026-10-08T20:00:00Z&end=2026-10-08T20:15:00Z'],
    ['markup', `date=${encodeURIComponent('<b>2026-10-08</b>')}&start=${encodeURIComponent(START)}&end=${encodeURIComponent(END)}`],
  ])('names no slot for a link with %s', (_case, query) => {
    expect(read(query)).toBeNull()
  })

  it('names no slot when the hospital’s zone cannot be used to check the day', () => {
    expect(read(sound, 'Mars/Olympus')).toBeNull()
    expect(read(sound, '')).toBeNull()
  })
})

describe('availability paths', () => {
  it('carries the day and the slot in the query string, escaped, and leaves an empty selection out', () => {
    expect(doctorAvailabilityPath('city-care', REF)).toBe(`/hospitals/city-care/doctors/${REF}/availability`)
    expect(doctorAvailabilityPath('city-care', REF, {})).toBe(`/hospitals/city-care/doctors/${REF}/availability`)
    expect(doctorAvailabilityPath('city-care', REF, { date: '2026-10-08' })).toBe(
      `/hospitals/city-care/doctors/${REF}/availability?date=2026-10-08`,
    )
    expect(doctorAvailabilityPath('city-care', REF, { date: '2026-10-08', slot: '2026-10-08T09:15:00+05:30' })).toBe(
      `/hospitals/city-care/doctors/${REF}/availability?date=2026-10-08&slot=2026-10-08T09%3A15%3A00%2B05%3A30`,
    )
    expect(
      doctorBookingPath('city-care', REF, { date: '2026-10-08', start: '2026-10-08T09:15:00+05:30', end: '2026-10-08T09:30:00+05:30' }),
    ).toBe(`/hospitals/city-care/doctors/${REF}/book?date=2026-10-08&start=2026-10-08T09%3A15%3A00%2B05%3A30&end=2026-10-08T09%3A30%3A00%2B05%3A30`)
    // A `+` left bare would come back as a space and break the offset: it is escaped.
    expect(doctorBookingPath('city-care', REF, { date: '2026-10-08', start: '2026-10-08T09:15:00+05:30', end: '2026-10-08T09:30:00+05:30' })).not.toContain('+')
  })
})

describe('what is sent', () => {
  it('sends only a range the server would take: two dates, in order, at most fourteen days', () => {
    expect(isSendableRange('2026-10-14', '2026-10-20')).toBe(true)
    expect(isSendableRange('2026-10-14', '2026-10-14')).toBe(true)
    expect(isSendableRange('2026-10-14', '2026-10-27')).toBe(true)
    expect(isSendableRange('2026-10-14', '2026-10-28')).toBe(false)
    expect(isSendableRange('2026-10-20', '2026-10-14')).toBe(false)
    expect(isSendableRange('', '')).toBe(false)
    expect(isSendableRange('2026-10-14', '')).toBe(false)
    expect(isSendableRange('2026-10-14', '2026-13-01')).toBe(false)
    expect(isSendableRange('2026-10-14T00:00', '2026-10-20')).toBe(false)
    expect(isSendableRange("2026-10-14' OR 1=1", '2026-10-20')).toBe(false)
  })
})

describe('what is shown of an answer', () => {
  const sound = availabilityOf('2026-10-07', '2026-10-13')

  it('keeps exactly the contract, slot by slot', () => {
    expect(toAvailability(sound)).toEqual(sound)
    expect(slotsOf('2026-10-08')).toEqual([
      { start: '2026-10-08T09:00:00+05:30', end: '2026-10-08T09:15:00+05:30' },
      { start: '2026-10-08T09:15:00+05:30', end: '2026-10-08T09:30:00+05:30' },
      { start: '2026-10-08T09:30:00+05:30', end: '2026-10-08T09:45:00+05:30' },
    ])
    // The fixture writes the hospital's own offset, on either side of a daylight-saving change.
    expect(instantOf('2026-10-30', '09:30', 'America/New_York')).toBe('2026-10-30T09:30:00-04:00')
    expect(instantOf('2026-11-02', '09:30', 'America/New_York')).toBe('2026-11-02T09:30:00-05:00')
    expect(instantOf('2026-10-08', '09:30', 'Pacific/Kiritimati')).toBe('2026-10-08T09:30:00+14:00')
  })

  it('ATTACK — drops everything a slot, a day or the answer carries beyond the contract', () => {
    const leaky = {
      ...sound,
      doctor: { id: 'DOC-SECRET', name: 'Asha Rao' },
      fee: '750.00',
      days: [
        {
          date: '2026-10-07',
          slots: [
            {
              start: '2026-10-07T11:30:00+05:30',
              end: '2026-10-07T11:45:00+05:30',
              id: 'SLOT-SECRET',
              status: 'booked',
              appointment_id: 'APPT-SECRET',
              patient: { name: 'Other Patient' },
              booked_by: 'BOOKER-SECRET',
            },
          ],
          leave_reason: 'LEAVE-SECRET',
          booked_count: 9,
        },
      ],
    }
    expect(toAvailability(leaky)).toEqual({
      ...sound,
      days: [{ date: '2026-10-07', slots: [{ start: '2026-10-07T11:30:00+05:30', end: '2026-10-07T11:45:00+05:30' }] }],
    })
    expect(JSON.stringify(toAvailability(leaky))).not.toMatch(/SECRET|750|Other Patient|status|booked/)
  })

  it('drops a slot that is not two instants in order, a day without a date, and a second entry for a date', () => {
    const ragged = {
      ...sound,
      days: [
        {
          date: '2026-10-07',
          slots: [
            { start: '2026-10-07T11:30:00+05:30', end: '2026-10-07T11:30:00+05:30' },
            { start: '2026-10-07T11:45:00+05:30', end: '2026-10-07T11:30:00+05:30' },
            { start: '2026-10-07T11:30:00', end: '2026-10-07T11:45:00' },
            { start: '2026-10-07', end: '2026-10-08' },
            { start: 1791345600000, end: 1791346500000 },
            'slot',
            null,
            { end: '2026-10-07T11:45:00+05:30' },
            { start: '2026-10-07T12:00:00+05:30', end: '2026-10-07T12:15:00+05:30' },
          ],
        },
        { date: '2026-10-07', slots: [{ start: '2026-10-07T13:00:00+05:30', end: '2026-10-07T13:15:00+05:30' }] },
        { slots: [{ start: '2026-10-08T09:00:00+05:30', end: '2026-10-08T09:15:00+05:30' }] },
        { date: 'tomorrow', slots: [] },
        { date: '2026-10-09', slots: 'none' },
        { date: '2026-10-10' },
        'day',
      ],
    }
    expect(toAvailability(ragged).days).toEqual([
      { date: '2026-10-07', slots: [{ start: '2026-10-07T12:00:00+05:30', end: '2026-10-07T12:15:00+05:30' }] },
      { date: '2026-10-09', slots: [] },
      { date: '2026-10-10', slots: [] },
    ])
  })

  it.each([
    ['not an object', 'Wednesday'],
    ['no body', null],
    ['a list', [sound]],
    ['no days', { ...sound, days: undefined }],
    ['days that are not a list', { ...sound, days: { '2026-10-07': [] } }],
    ['no zone', { ...sound, timezone: undefined }],
    ['an empty zone', { ...sound, timezone: '  ' }],
    ['no today', { ...sound, today: undefined }],
    ['a today that is not a date', { ...sound, today: 'Wednesday' }],
    ['no horizon', { ...sound, horizon_end: undefined }],
    ['a horizon that is not a date', { ...sound, horizon_end: '2026-13-01' }],
  ])('an answer with %s is a failed read', (_case, body) => {
    expect(() => toAvailability(body)).toThrow('Response is not availability')
  })

  it('fills in what the page can do without from what is there', () => {
    const sparse = { timezone: 'Asia/Kolkata', today: '2026-10-07', horizon_end: '2026-11-06', days: [] }
    expect(toAvailability(sparse)).toEqual({ ...sparse, min_lead_minutes: 0, start_date: '2026-10-07', end_date: '2026-10-07' })
    const odd = { ...sparse, min_lead_minutes: -5, start_date: 'soon', end_date: 7, days: [{ date: '2026-10-09', slots: [] }] }
    expect(toAvailability(odd)).toEqual({
      ...sparse,
      min_lead_minutes: 0,
      start_date: '2026-10-09',
      end_date: '2026-10-09',
      days: [{ date: '2026-10-09', slots: [] }],
    })
  })
})
