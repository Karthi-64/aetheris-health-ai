import { act, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import { afterEach, describe, expect, it } from 'vitest'
import { availabilityHandler, availabilityOf, availabilityRoute, horizonOf, slotsOf } from '@/test/availability'
import { doctorDirectory } from '@/test/doctorDirectory'
import { deferred, fail, headerOf, ok, serve, unreachable, type Handler, type Outcome, type Routes } from '@/test/fakeApi'
import { ashaRao, cardiology, cityCareHospital, meeraIyer, orthopaedics, vikramShah } from '@/test/fixtures'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

/**
 * The availability page: the free slots the server returns, on the hospital's
 * clock, and nothing else. The fake schedule's today is Wednesday 7 October
 * 2026 (`src/test/availability.ts`), with nothing left on it; Thursday has 3
 * slots, Friday 2, Saturday 1, Sunday 0, Monday 4, Tuesday 1. The horizon is
 * 30 days: Friday 6 November.
 */

const HOSPITAL = 'GET /hospitals/city-care'
const ASHA = `${HOSPITAL}/doctors/${ashaRao.ref}`
const AVAIL = `${ASHA}/availability`
const REFRESH = 'POST /auth/refresh'
const DOCTORS_PATH = '/hospitals/city-care/doctors'
const ASHA_PATH = `${DOCTORS_PATH}/${ashaRao.ref}`
const PATH = `${ASHA_PATH}/availability`

const TODAY = '2026-10-07'
const HORIZON = horizonOf()
const THU = '2026-10-08'
const FRI = '2026-10-09'
/** Friday's second slot, as the server writes it and as the address carries it. */
const FRI_1015 = '2026-10-09T10:15:00+05:30'
const FRI_1015_QUERY = encodeURIComponent(FRI_1015)

const THREE = [ashaRao, meeraIyer, vikramShah]

const cityCareWith = (doctors = THREE): Routes => ({
  [HOSPITAL]: ok(cityCareHospital),
  ...doctorDirectory('city-care', doctors, [cardiology, orthopaedics]),
})

function open(path = PATH) {
  signIn('access-1')
  return renderApp(path)
}

const main = () => screen.getByRole('main')
const title = (name: string) => screen.findByRole('heading', { level: 1, name })
const routeOf = (request: { method?: string; url?: string }) => `${request.method?.toUpperCase()} ${request.url}`
const notFound = () => fail(404, 'RESOURCE_NOT_FOUND', 'server wording that must not be shown')

/** The live line under "Choose a time". */
const status = () => screen.getByRole('status')
const dayStrip = () => screen.getByRole('list', { name: 'Days' })
const dayChips = () => within(dayStrip()).getAllByRole('button')
const dayChip = (name: string | RegExp) => screen.getByRole('button', { name })
const timeGrid = () => screen.getByRole('list', { name: /^Times on / })
const timeButtons = () => within(timeGrid()).getAllByRole('button')
const timeButton = (name: string | RegExp) => within(timeGrid()).getByRole('button', { name })
const earlier = () => screen.getByRole('button', { name: 'Earlier days' })
const later = () => screen.getByRole('button', { name: 'Later days' })
const continueLink = () => screen.getByRole('link', { name: 'Continue to booking' })
const continueButton = () => screen.getByRole('button', { name: 'Continue to booking' })
const daysLoaded = () => screen.findByRole('list', { name: 'Days' })
const skeletons = () => main().querySelectorAll('[data-slot="skeleton"]')
const paramsOf = (api: ReturnType<typeof serve>) => api.calls(AVAIL).map((request) => request.params)

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

describe('doctor availability — what the server returned, on the hospital’s clock', () => {
  it('shows skeletons while the availability loads, then the days', async () => {
    const { handler, answer } = deferred()
    serve({ ...cityCareWith(), [AVAIL]: handler })
    open()

    expect(await title('Availability')).toBeInTheDocument()
    expect(await screen.findByText('Loading availability…')).toHaveAttribute('role', 'status')
    expect(skeletons().length).toBeGreaterThan(0)
    expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
    expect(earlier()).toBeDisabled()
    expect(later()).toBeDisabled()
    // Nothing stands in for a slot while there is none: no time, no count, no day.
    expect(main()).not.toHaveTextContent(/\d{1,2}:\d{2}|\d+ (free )?slots?|No slots|Monday|Thursday/)

    answer(ok(availabilityOf(TODAY, '2026-10-13')))

    expect(await daysLoaded()).toBeInTheDocument()
    expect(skeletons()).toHaveLength(0)
    expect(screen.queryByText('Loading availability…')).not.toBeInTheDocument()
  })

  it('asks first for nothing in particular, and shows exactly the days and counts the server returned', async () => {
    const api = serve(cityCareWith())
    open()
    await daysLoaded()

    // The hospital, the doctor, then the availability — once, by public references, with the token and no dates.
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA, AVAIL])
    expect(api.calls(AVAIL)[0].params).toBeUndefined()
    expect(headerOf(api.calls(AVAIL)[0], 'Authorization')).toBe('Bearer access-1')

    // Every day of the week, in the server's order, each with the number of slots it returned.
    expect(dayChips().map((chip) => chip.getAttribute('aria-label'))).toEqual([
      'Wednesday 7 October 2026, no free slots',
      'Thursday 8 October 2026, 3 free slots',
      'Friday 9 October 2026, 2 free slots',
      'Saturday 10 October 2026, 1 free slot',
      'Sunday 11 October 2026, no free slots',
      'Monday 12 October 2026, 4 free slots',
      'Tuesday 13 October 2026, 1 free slot',
    ])
    expect(dayChips().map((chip) => chip.textContent)).toEqual([
      'Today7OctNo slots',
      'Thu8Oct3 slots',
      'Fri9Oct2 slots',
      'Sat10Oct1 slot',
      'Sun11OctNo slots',
      'Mon12Oct4 slots',
      'Tue13Oct1 slot',
    ])
    expect(screen.getByText('Wed 7 Oct – Tue 13 Oct')).toBeInTheDocument()

    // Whose availability, where, and on what clock.
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    expect(screen.getByText('Interventional Cardiology')).toBeInTheDocument()
    expect(screen.getByText('City Care Hospital')).toBeInTheDocument()
    expect(screen.getByText('Times are in the hospital’s local time (Asia/Kolkata)')).toBeInTheDocument()
    expect(screen.getByText('Seeing a free slot does not reserve it — it can be taken until the booking is confirmed.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Asha Rao' })).toHaveAttribute('href', ASHA_PATH)
    expect(document.title).toBe('Availability · Atheris Health')
    // At the first week there is nothing earlier; a month ahead there is.
    expect(earlier()).toBeDisabled()
    expect(later()).toBeEnabled()
  })

  it('selects by default the first day with a free slot, and shows its times on the hospital’s clock', async () => {
    const { router } = open()
    serve(cityCareWith())
    await daysLoaded()

    // Today has nothing: Thursday is the day in view, and the address says nothing yet.
    expect(dayChip(/^Thursday 8 October 2026/)).toHaveAttribute('aria-pressed', 'true')
    expect(dayChips().filter((chip) => chip.getAttribute('aria-pressed') === 'true')).toHaveLength(1)
    expect(router.state.location.search).toBe('')

    expect(timeGrid()).toHaveAttribute('aria-label', 'Times on Thursday 8 October 2026')
    expect(timeButtons().map((button) => button.textContent)).toEqual(['09:00', '09:15', '09:30'])
    expect(timeButtons().map((button) => button.getAttribute('aria-label'))).toEqual([
      '09:00 to 09:15, Thursday 8 October 2026',
      '09:15 to 09:30, Thursday 8 October 2026',
      '09:30 to 09:45, Thursday 8 October 2026',
    ])
    for (const button of timeButtons()) expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(status()).toHaveTextContent(/^3 free slots on Thursday 8 October 2026$/)
    // Nothing is chosen yet: no way on.
    expect(continueButton()).toBeDisabled()
    expect(screen.queryByRole('link', { name: 'Continue to booking' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear selection' })).not.toBeInTheDocument()
    expect(screen.getByText('Choose a day and a time to continue.')).toBeInTheDocument()
  })

  it('a day pressed goes into the address and its times are shown; one with nothing says so', async () => {
    const api = serve(cityCareWith())
    const { user, router } = open()
    await daysLoaded()

    await user.click(dayChip(/^Friday 9 October 2026/))

    expect(router.state.location.search).toBe(`?date=${FRI}`)
    expect(dayChip(/^Friday 9 October 2026/)).toHaveAttribute('aria-pressed', 'true')
    expect(dayChip(/^Thursday 8 October 2026/)).toHaveAttribute('aria-pressed', 'false')
    expect(timeButtons().map((button) => button.textContent)).toEqual(['10:00', '10:15'])
    expect(status()).toHaveTextContent(/^2 free slots on Friday 9 October 2026$/)
    // The pressed chip is the same element and keeps the focus.
    expect(dayChip(/^Friday 9 October 2026/)).toHaveFocus()

    await user.click(dayChip(/^Wednesday 7 October 2026/))

    expect(router.state.location.search).toBe(`?date=${TODAY}`)
    expect(screen.getByText('No free slots on this day')).toBeInTheDocument()
    expect(status()).toHaveTextContent(/^No free slots on Wednesday 7 October 2026$/)
    expect(screen.queryByRole('list', { name: /^Times on / })).not.toBeInTheDocument()
    expect(continueButton()).toBeDisabled()
    // Moving between days of the week asks the server for nothing more.
    expect(api.calls(AVAIL)).toHaveLength(1)
  })

  it('HOSPITAL CLOCK — a time is shown as the hospital’s wall clock, not as UTC and not as the browser’s', async () => {
    // Kiritimati is fourteen hours ahead of UTC; New York is behind it, and changes its clocks on 1 November.
    const kiritimati = {
      ...availabilityOf('2026-10-08', '2026-10-08', { timezone: 'Pacific/Kiritimati', today: '2026-10-08' }),
      days: [
        {
          date: '2026-10-08',
          slots: [
            // The same slot written with the hospital's offset, and as UTC: 09:30 on the hospital's clock either way.
            { start: '2026-10-08T09:30:00+14:00', end: '2026-10-08T09:45:00+14:00' },
            { start: '2026-10-07T20:00:00Z', end: '2026-10-07T20:15:00Z' },
          ],
        },
      ],
    }
    serve({ ...cityCareWith(), [AVAIL]: ok(kiritimati) })
    open()
    await daysLoaded()

    expect(timeButtons().map((button) => button.textContent)).toEqual(['09:30', '10:00'])
    expect(timeButtons().map((button) => button.getAttribute('aria-label'))).toEqual([
      '09:30 to 09:45, Thursday 8 October 2026',
      '10:00 to 10:15, Thursday 8 October 2026',
    ])
    expect(screen.getByText('Times are in the hospital’s local time (Pacific/Kiritimati)')).toBeInTheDocument()
    // Neither the UTC clock (19:30, 20:00) nor the browser's is anywhere on the page.
    const browserClock = new Date('2026-10-07T19:30:00Z').toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
    expect(main()).not.toHaveTextContent(/19:30|20:00|19:45|20:15/)
    if (browserClock !== '09:30') expect(main()).not.toHaveTextContent(browserClock)
    expect(dayChips().map((chip) => chip.getAttribute('aria-label'))).toEqual(['Thursday 8 October 2026, 2 free slots'])
  })

  it('HOSPITAL CLOCK — across a daylight-saving change, each day is on its own clock', async () => {
    const newYork = { timezone: 'America/New_York', today: '2026-10-30', slotsOn: () => ['09:30'] }
    serve({ ...cityCareWith(), [AVAIL]: availabilityHandler(newYork) })
    open()
    await daysLoaded()

    // Friday 30 October is UTC-4; Monday 2 November is UTC-5. Both read 09:30.
    expect(slotsOf('2026-10-30', newYork)[0].start).toBe('2026-10-30T09:30:00-04:00')
    expect(slotsOf('2026-11-02', newYork)[0].start).toBe('2026-11-02T09:30:00-05:00')
    expect(timeButtons().map((button) => button.textContent)).toEqual(['09:30'])
    expect(status()).toHaveTextContent('1 free slot on Friday 30 October 2026')

    screen.getByRole('button', { name: /^Monday 2 November 2026/ }).click()
    await waitFor(() => expect(status()).toHaveTextContent('1 free slot on Monday 2 November 2026'))
    expect(timeButtons().map((button) => button.textContent)).toEqual(['09:30'])
    expect(main()).not.toHaveTextContent(/13:30|14:30|08:30|10:30/)
  })

  it('a time pressed goes into the address, opens the way to booking with the slot in its link, and Clear selection takes it out', async () => {
    serve(cityCareWith())
    const { user, router } = open()
    await daysLoaded()
    await user.click(dayChip(/^Friday 9 October 2026/))

    await user.click(timeButton(/^10:15 to 10:30/))

    expect(router.state.location.search).toBe(`?date=${FRI}&slot=${FRI_1015_QUERY}`)
    expect(timeButton(/^10:15 to 10:30/)).toHaveAttribute('aria-pressed', 'true')
    expect(timeButton(/^10:00 to 10:15/)).toHaveAttribute('aria-pressed', 'false')
    expect(timeButton(/^10:15 to 10:30/)).toHaveFocus()
    expect(screen.getByText('Friday 9 October 2026, 10:15 to 10:30')).toBeInTheDocument()
    expect(continueLink()).toHaveAttribute(
      'href',
      `${ASHA_PATH}/book?date=${FRI}&start=${FRI_1015_QUERY}&end=${encodeURIComponent('2026-10-09T10:30:00+05:30')}`,
    )
    expect(screen.queryByRole('button', { name: 'Continue to booking' })).not.toBeInTheDocument()

    // Another time replaces the first.
    await user.click(timeButton(/^10:00 to 10:15/))
    expect(router.state.location.search).toBe(`?date=${FRI}&slot=${encodeURIComponent('2026-10-09T10:00:00+05:30')}`)
    expect(timeButton(/^10:15 to 10:30/)).toHaveAttribute('aria-pressed', 'false')
    expect(timeButton(/^10:00 to 10:15/)).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('button', { name: 'Clear selection' }))

    expect(router.state.location.search).toBe(`?date=${FRI}`)
    for (const button of timeButtons()) expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(continueButton()).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Clear selection' })).not.toBeInTheDocument()
    // The button that was pressed has gone: focus is at the top of the times.
    expect(screen.getByRole('heading', { level: 2, name: 'Choose a time' })).toHaveFocus()

    // Pressing a chosen time again lets go of it too.
    await user.click(timeButton(/^10:00 to 10:15/))
    await user.click(timeButton(/^10:00 to 10:15/))
    expect(router.state.location.search).toBe(`?date=${FRI}`)
    expect(continueButton()).toBeDisabled()
  })

  it('RELOAD — the day and the time come back from the address, with one request', async () => {
    const api = serve(cityCareWith())
    open(`${PATH}?date=${FRI}&slot=${FRI_1015_QUERY}`)
    await daysLoaded()

    expect(dayChip(/^Friday 9 October 2026/)).toHaveAttribute('aria-pressed', 'true')
    expect(timeButton(/^10:15 to 10:30/)).toHaveAttribute('aria-pressed', 'true')
    expect(continueLink()).toBeInTheDocument()
    expect(screen.queryByText('That time is no longer free.')).not.toBeInTheDocument()
    expect(paramsOf(api)).toEqual([undefined])
  })

  it('a day in the address that this week does not cover: the week that holds it is read, by its dates', async () => {
    const api = serve(cityCareWith())
    open(`${PATH}?date=2026-10-22`)

    expect(await screen.findByRole('button', { name: /^Thursday 22 October 2026/ })).toHaveAttribute('aria-pressed', 'true')
    expect(dayChips().map((chip) => chip.textContent?.slice(0, 5))).toEqual(['Wed21', 'Thu22', 'Fri23', 'Sat24', 'Sun25', 'Mon26', 'Tue27'])
    expect(screen.getByText('Wed 21 Oct – Tue 27 Oct')).toBeInTheDocument()
    // First the server's own week, which says where today is; then the week asked for.
    expect(paramsOf(api)).toEqual([undefined, { start_date: '2026-10-21', end_date: '2026-10-27' }])
    expect(earlier()).toBeEnabled()
    expect(later()).toBeEnabled()
  })

  it.each([
    ['yesterday', '2026-10-06'],
    ['the day after the horizon', '2026-11-07'],
    ['next year', '2027-10-07'],
    ['a month that does not exist', '2026-13-01'],
    ['a word', 'tomorrow'],
    ['a date and a time', '2026-10-08T00:00'],
    ['markup', '<b>2026-10-08</b>'],
  ])('a day in the address that is %s falls back to today’s week and leaves the address, and is never sent', async (_case, date) => {
    const api = serve(cityCareWith())
    const { router } = open(`${PATH}?date=${encodeURIComponent(date)}&slot=${FRI_1015_QUERY}`)
    await daysLoaded()

    await waitFor(() => expect(router.state.location.search).toBe(''))
    expect(dayChip(/^Thursday 8 October 2026/)).toHaveAttribute('aria-pressed', 'true')
    expect(paramsOf(api)).toEqual([undefined])
    expect(main()).not.toHaveTextContent(date)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('a time in the address that is no longer free is dropped from it, and said to be', async () => {
    const taken = encodeURIComponent('2026-10-09T12:00:00+05:30')
    serve(cityCareWith())
    const { user, router } = open(`${PATH}?date=${FRI}&slot=${taken}`)
    await daysLoaded()

    expect(await screen.findByText('That time is no longer free.')).toBeInTheDocument()
    await waitFor(() => expect(router.state.location.search).toBe(`?date=${FRI}`))
    expect(dayChip(/^Friday 9 October 2026/)).toHaveAttribute('aria-pressed', 'true')
    for (const button of timeButtons()) expect(button).toHaveAttribute('aria-pressed', 'false')
    expect(continueButton()).toBeDisabled()
    expect(main()).not.toHaveTextContent('12:00')

    // Choosing a time that is free puts the note away.
    await user.click(timeButton(/^10:00 to 10:15/))
    expect(screen.queryByText('That time is no longer free.')).not.toBeInTheDocument()
    expect(continueLink()).toBeInTheDocument()
  })

  it('the note about a time that is gone belongs to its day: another day reached by back does not carry it', async () => {
    const taken = encodeURIComponent('2026-10-09T12:00:00+05:30')
    serve(cityCareWith())
    const { router } = open(`${PATH}?date=${THU}`)
    await daysLoaded()

    await act(() => router.navigate(`${PATH}?date=${FRI}&slot=${taken}`))
    expect(await screen.findByText('That time is no longer free.')).toBeInTheDocument()
    await waitFor(() => expect(router.state.location.search).toBe(`?date=${FRI}`))

    await act(() => router.navigate(-1))

    await waitFor(() => expect(dayChip(/^Thursday 8 October 2026/)).toHaveAttribute('aria-pressed', 'true'))
    expect(screen.queryByText('That time is no longer free.')).not.toBeInTheDocument()
  })

  it('a time in the address that was never a time is dropped without a word', async () => {
    serve(cityCareWith())
    const { router } = open(`${PATH}?date=${FRI}&slot=${encodeURIComponent('<script>window.pwned=1</script>')}`)
    await daysLoaded()

    await waitFor(() => expect(router.state.location.search).toBe(`?date=${FRI}`))
    expect(screen.queryByText('That time is no longer free.')).not.toBeInTheDocument()
    expect(main().innerHTML).not.toContain('script>')
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
  })

  describe('turning the weeks', () => {
    it('asks for each week by its dates, is held at today and at the horizon, and each week is a step back', async () => {
      const api = serve(cityCareWith())
      const { user, router } = open()
      await daysLoaded()
      expect(earlier()).toBeDisabled()

      await user.click(later())

      expect(await screen.findByRole('button', { name: /^Wednesday 14 October 2026/ })).toHaveAttribute('aria-pressed', 'true')
      expect(router.state.location.search).toBe('?date=2026-10-14')
      expect(paramsOf(api).at(-1)).toEqual({ start_date: '2026-10-14', end_date: '2026-10-20' })
      expect(screen.getByText('Wed 14 Oct – Tue 20 Oct')).toBeInTheDocument()
      expect(screen.getByRole('heading', { level: 2, name: 'Choose a day' })).toHaveFocus()
      expect(earlier()).toBeEnabled()
      expect(later()).toBeEnabled()

      await user.click(later())
      await user.click(later())
      await user.click(later())

      // The last week is three days long: Wednesday 4 to Friday 6 November, and no further.
      expect(await screen.findByRole('button', { name: /^Wednesday 4 November 2026/ })).toHaveAttribute('aria-pressed', 'true')
      expect(dayChips()).toHaveLength(3)
      expect(dayChips().at(-1)).toHaveAccessibleName(/^Friday 6 November 2026/)
      expect(screen.getByText('Wed 4 Nov – Fri 6 Nov')).toBeInTheDocument()
      expect(later()).toBeDisabled()
      expect(paramsOf(api)).toEqual([
        undefined,
        { start_date: '2026-10-14', end_date: '2026-10-20' },
        { start_date: '2026-10-21', end_date: '2026-10-27' },
        { start_date: '2026-10-28', end_date: '2026-11-03' },
        { start_date: '2026-11-04', end_date: HORIZON },
      ])

      // Back is the week before.
      await act(() => router.navigate(-1))
      expect(await screen.findByText('Wed 28 Oct – Tue 3 Nov')).toBeInTheDocument()
      expect(router.state.location.search).toBe('?date=2026-10-28')

      await user.click(earlier())
      await user.click(earlier())
      await user.click(earlier())

      // Back at the first week: today's own, read a moment ago and not asked for again.
      expect(await screen.findByText('Wed 7 Oct – Tue 13 Oct')).toBeInTheDocument()
      expect(router.state.location.search).toBe(`?date=${TODAY}`)
      expect(earlier()).toBeDisabled()
      expect(api.calls(AVAIL)).toHaveLength(5)
      // Every request was one bookable week; nothing before today or past the horizon was ever asked for.
      for (const params of paramsOf(api).slice(1)) {
        const { start_date: start, end_date: end } = params as { start_date: string; end_date: string }
        expect(start >= TODAY && end <= HORIZON && end >= start).toBe(true)
      }
    })

    it('shows skeletons while the next week loads, and lets go of the chosen time', async () => {
      const api = serve(cityCareWith())
      const { user, router } = open(`${PATH}?date=${FRI}&slot=${FRI_1015_QUERY}`)
      await daysLoaded()
      expect(continueLink()).toBeInTheDocument()

      const { handler, answer } = deferred()
      api.on({ [AVAIL]: handler })
      await user.click(later())

      expect(await screen.findByText('Loading availability…')).toHaveAttribute('role', 'status')
      expect(skeletons().length).toBeGreaterThan(0)
      expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
      expect(earlier()).toBeDisabled()
      expect(later()).toBeDisabled()
      expect(continueButton()).toBeDisabled()
      expect(router.state.location.search).toBe('?date=2026-10-14')

      answer(ok(availabilityOf('2026-10-14', '2026-10-20')))

      expect(await screen.findByRole('button', { name: /^Wednesday 14 October 2026/ })).toBeInTheDocument()
      expect(skeletons()).toHaveLength(0)
      expect(status()).toHaveTextContent('No free slots on Wednesday 14 October 2026')
    })
  })

  describe('nothing free', () => {
    it('EMPTY DAY — says so, and leaves the other days there to choose', async () => {
      serve(cityCareWith())
      open(`${PATH}?date=2026-10-11`)
      await daysLoaded()

      expect(screen.getByText('No free slots on this day')).toBeInTheDocument()
      expect(status()).toHaveTextContent(/^No free slots on Sunday 11 October 2026$/)
      expect(screen.queryByText('No free slots in these days')).not.toBeInTheDocument()
      expect(dayChip(/^Monday 12 October 2026/)).toHaveTextContent('4 slots')
    })

    it('EMPTY WEEK — says so once, with every day marked, and no time or count invented', async () => {
      serve({ ...cityCareWith(), [AVAIL]: availabilityHandler({ slotsOn: () => [] }) })
      open()
      await daysLoaded()

      expect(screen.getByText('No free slots in these days')).toBeInTheDocument()
      expect(status()).toHaveTextContent(/^No free slots from Wed 7 Oct to Tue 13 Oct$/)
      expect(screen.queryByText('No free slots on this day')).not.toBeInTheDocument()
      expect(dayChips()).toHaveLength(7)
      for (const chip of dayChips()) expect(chip).toHaveTextContent('No slots')
      expect(screen.queryByRole('list', { name: /^Times on / })).not.toBeInTheDocument()
      expect(main()).not.toHaveTextContent(/\d{1,2}:\d{2}/)
      expect(continueButton()).toBeDisabled()
      // The next week may well have something.
      expect(later()).toBeEnabled()
    })

    it('EMPTY ANSWER — no days at all is the same, and nothing breaks', async () => {
      const empty = { ...availabilityOf(TODAY, TODAY), days: [] }
      serve({ ...cityCareWith(), [AVAIL]: ok(empty) })
      open()

      expect(await screen.findByText('No free slots in these days')).toBeInTheDocument()
      expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
      expect(continueButton()).toBeDisabled()
      expect(main()).not.toHaveTextContent(/undefined|null|NaN/)
    })
  })

  describe('when it cannot load', () => {
    afterEach(() => onlineManager.setOnline(true))

    const GATEWAY_PAGE: Outcome = {
      status: 503,
      data: '<html><body><h1>503 Service Temporarily Unavailable</h1><script>window.pwned=1</script>nginx</body></html>',
    }

    it('OFFLINE and 503 are told apart, in the app’s words, and each Retry asks exactly once more', async () => {
      const api = serve({ ...cityCareWith(), [AVAIL]: GATEWAY_PAGE })
      const { user } = open()

      const message = 'We could not load the availability. Please try again.'
      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent(/No connection|internet connection/)
      expect(document.body).not.toHaveTextContent(/503|Service Temporarily|nginx|Network Error|Request failed/)
      expect((window as { pwned?: unknown }).pwned).toBeUndefined()
      // A failure is never dressed up as an empty week.
      expect(document.body).not.toHaveTextContent(/No free slots/)
      expect(status()).toBeEmptyDOMElement()
      expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
      // The doctor is known: the page is still theirs.
      expect(screen.getByRole('heading', { level: 1, name: 'Availability' })).toBeInTheDocument()
      expect(screen.getByText('Asha Rao')).toBeInTheDocument()

      api.on({ [AVAIL]: unreachable })
      await user.click(screen.getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No connection'))
      expect(screen.getByRole('alert')).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
      expect(screen.queryByText(message)).not.toBeInTheDocument()
      expect(api.calls(AVAIL)).toHaveLength(2)

      api.on(cityCareWith())
      await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await daysLoaded()).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api.calls(AVAIL)).toHaveLength(3)
      expect(screen.getByRole('heading', { level: 2, name: 'Choose a day' })).toHaveFocus()
    })

    it('OFFLINE — a request held back for lack of a network says so, and resumes by itself', async () => {
      serve(cityCareWith())
      const { router } = open(ASHA_PATH)
      await title('Asha Rao')

      onlineManager.setOnline(false)
      await act(() => router.navigate(PATH))

      expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
      expect(screen.queryByText('Loading availability…')).not.toBeInTheDocument()

      act(() => onlineManager.setOnline(true))

      expect(await daysLoaded()).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it.each([
      ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot show this in the app right now\. Please try again later\.$/],
      ['the rate limit (429)', fail(429, 'RATE_LIMITED', 'server wording'), /^Too many requests\. Please wait a moment and try again\.$/],
      ['a refused parameter (422)', fail(422, 'VALIDATION_ERROR', 'server wording'), /^We could not load the availability\. Please try again\.$/],
      // The doctor was just read: a 404 for the availability alone is availability that did not load.
      ['a 404 for the availability alone', fail(404, 'RESOURCE_NOT_FOUND', 'server wording'), /^We could not load the availability\. Please try again\.$/],
      ['a failure on the server (500)', fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)'), /^We could not load the availability\. Please try again\.$/],
    ])('shows a safe message for %s', async (_case, outcome, message) => {
      serve({ ...cityCareWith(), [AVAIL]: outcome })
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent(/server wording|Traceback/)
      expect(screen.queryByText('This doctor is not available')).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    })

    it.each([
      ['that is not availability at all', 'Wednesday'],
      ['with no body', null],
      ['that is a list', [availabilityOf(TODAY, '2026-10-13')]],
      ['with no days', { ...availabilityOf(TODAY, '2026-10-13'), days: undefined }],
      ['with days that are not a list', { ...availabilityOf(TODAY, '2026-10-13'), days: 'Mon-Fri 9-5' }],
      ['with no zone', { ...availabilityOf(TODAY, '2026-10-13'), timezone: undefined }],
      ['with no today', { ...availabilityOf(TODAY, '2026-10-13'), today: undefined }],
      ['with a today that is not a date', { ...availabilityOf(TODAY, '2026-10-13'), today: 'Wednesday' }],
      ['with no horizon', { ...availabilityOf(TODAY, '2026-10-13'), horizon_end: null }],
    ])('a 200 %s is a failure with a retry, not a broken page and not an invented week', async (_case, body) => {
      const api = serve({ ...cityCareWith(), [AVAIL]: ok(body) })
      const { user } = open()

      expect(await screen.findByRole('alert')).toHaveTextContent('We could not load the availability. Please try again.')
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
      expect(status()).toBeEmptyDOMElement()
      expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
      expect(main()).not.toHaveTextContent(/Mon-Fri|Wednesday|undefined|null/)

      api.on(cityCareWith())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await daysLoaded()).toBeInTheDocument()
    })

    it('DATES REFUSED — a week the server will no longer give says the date has gone, and "Go to today" starts again from nothing in particular', async () => {
      // Midnight passed on the server between the two requests: the week the address names is behind it now.
      const api = serve({
        ...cityCareWith(),
        [AVAIL]: (config) =>
          config.params ? fail(400, 'BUSINESS_RULE_VIOLATION', 'Outside the bookable window.') : availabilityHandler()(config),
      })
      const { user, router } = open(`${PATH}?date=2026-10-15&slot=${encodeURIComponent('2026-10-15T09:00:00+05:30')}`)

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('This date is no longer available')
      expect(alert).toHaveTextContent('The days that can be booked have moved on. Start again from today.')
      expect(document.body).not.toHaveTextContent('Outside the bookable window')
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
      expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
      expect(status()).toBeEmptyDOMElement()
      expect(continueButton()).toBeDisabled()
      expect(paramsOf(api)).toEqual([undefined, { start_date: '2026-10-14', end_date: '2026-10-20' }])

      await user.click(screen.getByRole('button', { name: 'Go to today' }))

      expect(await daysLoaded()).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(dayChip(/^Thursday 8 October 2026/)).toHaveAttribute('aria-pressed', 'true')
      expect(screen.getByRole('heading', { level: 2, name: 'Choose a day' })).toHaveFocus()
      // Asked once more, for nothing in particular: the first answer is not reused, today may have moved.
      expect(paramsOf(api)).toEqual([undefined, { start_date: '2026-10-14', end_date: '2026-10-20' }, undefined])
      expect(screen.queryByText('That time is no longer free.')).not.toBeInTheDocument()
    })
  })

  describe('not found and never sent', () => {
    it('NOT FOUND — an unknown or hidden doctor is the doctor’s own page, and the availability is never asked for', async () => {
      const api = serve({ ...cityCareWith(), [ASHA]: notFound() })
      open()

      expect(await title('This doctor is not available')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Browse doctors' })).toHaveAttribute('href', DOCTORS_PATH)
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA])
      expect(document.body).not.toHaveTextContent(/server wording|Choose a day|Choose a time/)
    })

    it('NOT FOUND — an unknown hospital is the hospital’s own page, and neither doctor nor availability is asked for', async () => {
      const api = serve({ [HOSPITAL]: notFound() })
      open()

      expect(await title('This hospital is not available')).toBeInTheDocument()
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL])
    })

    it.each([
      ['words', 'asha-rao'],
      ['a path step', '..%2F..%2Fme'],
      ['a reference with something after it', `${ashaRao.ref}0`],
      ['a reference with a query after it', `${ashaRao.ref}%3Fx=1`],
    ])('ATTACK — a doctor reference that is %s is never sent, with or without dates in the address', async (_case, ref) => {
      const api = serve({ [HOSPITAL]: ok(cityCareHospital) })
      open(`${DOCTORS_PATH}/${ref}/availability?date=2026-10-14`)

      expect(await title('This doctor is not available')).toBeInTheDocument()
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL])
    })

    it.each([
      ['a path step', '..%2F..%2Fme'],
      ['a query in the segment', 'city-care%3Fx=1'],
      ['more than a reference holds', 'a'.repeat(101)],
    ])('ATTACK — a hospital reference that is %s sends nothing at all', async (_case, ref) => {
      const api = serve({})
      open(`/hospitals/${ref}/doctors/${ashaRao.ref}/availability`)

      expect(await title('This hospital is not available')).toBeInTheDocument()
      expect(api.sent).toHaveLength(0)
    })
  })

  describe('what never reaches the screen', () => {
    it('ATTACK — a slot, a day or an answer carrying more than the contract: only the start and the end of a free slot are shown', async () => {
      const leaky = {
        ...availabilityOf(TODAY, '2026-10-13'),
        doctor: { id: '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55', name: 'Asha Rao', fee: '750.00' },
        consultation_fee: '750.00',
        capacity: 1234,
        booked: 567,
        days: [
          {
            date: THU,
            slots: [
              {
                start: '2026-10-08T11:30:00+05:30',
                end: '2026-10-08T11:45:00+05:30',
                id: 'SLOT-SECRET',
                slot_id: '7a7a7a7a-1111-4222-8333-444444444444',
                status: 'booked',
                appointment_id: 'APPT-SECRET',
                patient: { name: 'Other Patient', mrn: 'MRN-SECRET' },
                booked_by: 'BOOKER-SECRET',
                price: '750.00',
              },
            ],
            leave_reason: 'LEAVE-SECRET',
            booked_count: 890,
            total_slots: 3232,
          },
        ],
      }
      serve({ ...cityCareWith(), [AVAIL]: ok(leaky) })
      open()
      await daysLoaded()

      // The slot is shown by its times — the server sends only free ones, so its "status" is not the app's to read.
      expect(timeButtons().map((button) => button.textContent)).toEqual(['11:30'])
      expect(dayChips().map((chip) => chip.getAttribute('aria-label'))).toEqual(['Thursday 8 October 2026, 1 free slot'])
      const page = document.body.innerHTML
      for (const secret of ['5f0c2a9e', '7a7a7a7a', 'SLOT-SECRET', 'APPT-SECRET', 'Other Patient', 'MRN-SECRET', 'BOOKER-SECRET', 'LEAVE-SECRET', '750', 'booked']) {
        expect(page).not.toContain(secret)
      }
      expect(main()).not.toHaveTextContent(/\bfee\b|₹|capacity|total|status|1234|567|890|3232/)
      expect(document.body.textContent).not.toMatch(UUID)
      for (const element of document.querySelectorAll('*')) {
        for (const attribute of element.attributes) {
          if (attribute.name !== 'href') expect(attribute.value).not.toMatch(UUID)
        }
      }
      expect(document.querySelector('main img, main [src], main [style]')).toBeNull()
    })

    it('ATTACK — markup in the zone is shown as the characters it is, and the times are still the hospital’s', async () => {
      const hostile = { ...availabilityOf(TODAY, '2026-10-13'), timezone: '<img src=x onerror=window.pwned=1>' }
      serve({ ...cityCareWith(), [AVAIL]: ok(hostile) })
      open()
      await daysLoaded()

      expect(screen.getByText('Times are in the hospital’s local time (<img src=x onerror=window.pwned=1>)')).toBeInTheDocument()
      expect(main().querySelector('script, iframe, img, b, i, u, style, [onerror], [onclick]')).toBeNull()
      expect((window as { pwned?: unknown }).pwned).toBeUndefined()
      // A zone the browser does not know: the clock is still the one the server wrote the slots with.
      expect(timeButtons().map((button) => button.textContent)).toEqual(['09:00', '09:15', '09:30'])
    })

    it('a slot that is not two instants in order is left out, and the count says so', async () => {
      const ragged = {
        ...availabilityOf(TODAY, '2026-10-13'),
        days: [
          {
            date: THU,
            slots: [
              { start: '2026-10-08T09:00:00+05:30', end: '2026-10-08T09:15:00+05:30' },
              { start: '2026-10-08T09:30:00+05:30', end: '2026-10-08T09:30:00+05:30' },
              { start: '2026-10-08T10:00:00', end: '2026-10-08T10:15:00' },
              { start: '2026-10-08T10:30:00+05:30' },
              'all day',
            ],
          },
        ],
      }
      serve({ ...cityCareWith(), [AVAIL]: ok(ragged) })
      open()
      await daysLoaded()

      expect(timeButtons().map((button) => button.textContent)).toEqual(['09:00'])
      expect(status()).toHaveTextContent(/^1 free slot on Thursday 8 October 2026$/)
      expect(main()).not.toHaveTextContent(/09:30|10:00|10:30|all day/)
    })
  })

  describe('session', () => {
    it('an expired token: one refresh, the availability asked for again with the new token, the days shown', async () => {
      signIn('access-old')
      const seen: string[] = []
      const directory = cityCareWith()
      const onlyNew: Handler = (config: InternalAxiosRequestConfig) => {
        seen.push(String(headerOf(config, 'Authorization')))
        return headerOf(config, 'Authorization') === 'Bearer access-new'
          ? (directory[AVAIL] as Handler)(config)
          : fail(401, 'AUTHENTICATION_REQUIRED', 'server wording')
      }
      const api = serve({ ...directory, [AVAIL]: onlyNew, [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }) })
      const { router } = renderApp(ASHA_PATH)
      await title('Asha Rao')
      await act(() => router.navigate(PATH))

      expect(await daysLoaded()).toBeInTheDocument()
      expect(api.calls(REFRESH)).toHaveLength(1)
      expect(seen).toEqual(['Bearer access-old', 'Bearer access-new'])
      // The retry is the same question: nothing in particular.
      expect(paramsOf(api)).toEqual([undefined, undefined])
      expect(router.state.location.pathname).toBe(PATH)
      expect(document.body).not.toHaveTextContent('server wording')
      expect(isSignedIn()).toBe(true)
    })

    it('ATTACK — a dead session: one refresh, one sign-out, nothing of the availability shown', async () => {
      signIn('access-old')
      const api = serve({
        [HOSPITAL]: ok(cityCareHospital),
        [ASHA]: ok(ashaRao),
        [AVAIL]: fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'),
        [REFRESH]: fail(401, 'UNAUTHORIZED'),
      })
      const { router } = renderApp(PATH)

      expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/login')
      expect(isSignedIn()).toBe(false)
      expect(api.calls(REFRESH)).toHaveLength(1)
      expect(api.calls(AVAIL)).toHaveLength(1)
      expect(document.body).not.toHaveTextContent(/City Care|Asha|server wording|could not load|slot/)
    })
  })

  describe('keyboard and screen reader', () => {
    it('the count is one live region that stays in the page and changes its text: loading, a day, another day', async () => {
      const { handler, answer } = deferred()
      const api = serve({ ...cityCareWith(), [AVAIL]: handler })
      const { user } = open()

      const live = await screen.findByText('Loading availability…')
      expect(live).toHaveAttribute('role', 'status')
      expect(live.closest('[aria-hidden="true"]')).toBeNull()

      api.on(cityCareWith())
      answer(ok(availabilityOf(TODAY, '2026-10-13')))
      await daysLoaded()

      // The same node, so the change is announced; a replaced node would not be.
      expect(screen.getAllByRole('status')).toEqual([live])
      expect(live).toHaveTextContent(/^3 free slots on Thursday 8 October 2026$/)

      await user.click(dayChip(/^Monday 12 October 2026/))
      await waitFor(() => expect(live).toHaveTextContent(/^4 free slots on Monday 12 October 2026$/))
      expect(screen.getAllByRole('status')).toEqual([live])
    })

    it('works from the keyboard alone: a day, a time, and on to booking, with every control named', async () => {
      serve(cityCareWith())
      const { user, router } = open()
      await daysLoaded()

      // Every day and every time is a real button that says whether it is pressed.
      for (const button of [...dayChips(), ...timeButtons()]) {
        expect(button.tagName).toBe('BUTTON')
        expect(button).toHaveAttribute('aria-pressed')
        expect(button).toHaveAccessibleName()
      }
      for (const element of document.querySelectorAll('[tabindex]')) expect(element).toHaveAttribute('tabindex', '-1')
      for (const icon of document.querySelectorAll('svg')) expect(icon).toHaveAttribute('aria-hidden', 'true')
      for (const control of document.querySelectorAll<HTMLElement>('a, button')) expect(control).toHaveAccessibleName()
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)

      const friday = dayChip(/^Friday 9 October 2026/)
      for (let presses = 0; presses < 20 && document.activeElement !== friday; presses++) await user.tab()
      expect(friday).toHaveFocus()

      await user.keyboard('{Enter}')
      expect(router.state.location.search).toBe(`?date=${FRI}`)
      expect(friday).toHaveFocus()

      const time = timeButton(/^10:15 to 10:30/)
      for (let presses = 0; presses < 20 && document.activeElement !== time; presses++) await user.tab()
      expect(time).toHaveFocus()

      await user.keyboard('{Enter}')
      expect(time).toHaveAttribute('aria-pressed', 'true')
      expect(router.state.location.search).toBe(`?date=${FRI}&slot=${FRI_1015_QUERY}`)

      await user.tab()
      expect(continueLink()).toHaveFocus()
      await user.tab()
      expect(screen.getByRole('button', { name: 'Clear selection' })).toHaveFocus()

      await user.tab({ shift: true })
      await user.keyboard('{Enter}')

      expect(await title('Booking')).toHaveFocus()
      expect(router.state.location.pathname).toBe(`${ASHA_PATH}/book`)
      expect(router.state.location.search).toBe(`?date=${FRI}&start=${FRI_1015_QUERY}&end=${encodeURIComponent('2026-10-09T10:30:00+05:30')}`)
    })

    it('every control is a 44 px target, and there is no table, form, field or image', async () => {
      serve(cityCareWith())
      open()
      await daysLoaded()

      const touchSized = /(^|\s)(h-11|min-h-11|size-11)(\s|$)/
      for (const control of main().querySelectorAll('a, button')) expect(control.className).toMatch(touchSized)
      expect(main().querySelector('table, form, input, select, textarea, img, time')).toBeNull()
    })
  })

  describe('moving on', () => {
    it('moving from one doctor to another starts a fresh page: nothing of the first is left on the second', async () => {
      const api = serve({
        ...cityCareWith(),
        ...availabilityRoute('city-care', vikramShah.ref, { slotsOn: (date) => (date === THU ? ['15:00'] : []) }),
      })
      const { router } = open(`${PATH}?date=${FRI}&slot=${FRI_1015_QUERY}`)
      await daysLoaded()
      expect(continueLink()).toBeInTheDocument()

      await act(() => router.navigate(`${DOCTORS_PATH}/${vikramShah.ref}/availability`))

      expect(await screen.findByText('Vikram Shah')).toBeInTheDocument()
      expect(await screen.findByRole('button', { name: /^15:00 to 15:15/ })).toBeInTheDocument()
      expect(main()).not.toHaveTextContent(/Asha|Cardiology|10:15/)
      expect(continueButton()).toBeDisabled()
      expect(api.calls(`${HOSPITAL}/doctors/${vikramShah.ref}/availability`)[0].params).toBeUndefined()
    })
  })
})
