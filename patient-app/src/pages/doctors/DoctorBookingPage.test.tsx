import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'
import { ApiError } from '@atheris/api-core'
import { classifyBookingError, isRetriable, toBookedAppointment } from '@/api/appointments'
import { api as client } from '@/api/client'
import {
  appointmentOf,
  appointmentRef,
  bookingEndpoint,
  LOST,
  refusals,
  type FakeBookingOptions,
  type ScriptedAnswer,
} from '@/test/booking'
import { doctorDirectory } from '@/test/doctorDirectory'
import { deferred, fail, noContent, ok, serve, unreachable, type Routes } from '@/test/fakeApi'
import { ashaRao, cardiology, cityCareHospital, meeraIyer, orthopaedics, vikramShah } from '@/test/fixtures'
import { myAppointmentsEndpoints } from '@/test/myAppointments'
import { renderApp, signIn } from '@/test/renderApp'

/**
 * Where "Continue to booking" leads: the chosen slot, shown back on the
 * hospital's clock for review, and booked by one explicit confirmation. The
 * tests here hold the page to three things: it sends exactly one request per
 * booking (and the very same one on every retry), it says "booked" only after
 * the server has, and nothing the address or a response carries reaches the
 * screen or the network unless the contract names it.
 */

const HOSPITAL = 'GET /hospitals/city-care'
const ASHA = `${HOSPITAL}/doctors/${ashaRao.ref}`
const REFRESH = 'POST /auth/refresh'
const DOCTORS_PATH = '/hospitals/city-care/doctors'
const ASHA_PATH = `${DOCTORS_PATH}/${ashaRao.ref}`
const BOOK_PATH = `${ASHA_PATH}/book`
const AVAILABILITY_PATH = `${ASHA_PATH}/availability`

const START = '2026-10-09T10:15:00+05:30'
const END = '2026-10-09T10:30:00+05:30'
const q = encodeURIComponent
const SOUND = `date=2026-10-09&start=${q(START)}&end=${q(END)}`
const OTHER_SLOT = `date=2026-10-09&start=${q('2026-10-09T10:00:00+05:30')}&end=${q(START)}`

const KEY = /^[A-Za-z0-9_-]{16,64}$/

const cityCareWith = (): Routes => ({
  [HOSPITAL]: ok(cityCareHospital),
  ...doctorDirectory('city-care', [ashaRao, meeraIyer, vikramShah], [cardiology, orthopaedics]),
})

/** City Care with Asha Rao's booking endpoint behind it. */
function setup(options: FakeBookingOptions = {}, more: Routes = {}) {
  const booking = bookingEndpoint(cityCareHospital, ashaRao, options)
  const api = serve({ ...cityCareWith(), ...booking.routes, ...more })
  return { api, booking }
}

function open(query = SOUND) {
  signIn('access-1')
  return renderApp(`${BOOK_PATH}?${query}`)
}

const main = () => screen.getByRole('main')
const title = (name: string | RegExp) => screen.findByRole('heading', { level: 1, name })
const routeOf = (request: { method?: string; url?: string }) => `${request.method?.toUpperCase()} ${request.url}`
const confirmButton = () => screen.findByRole('button', { name: 'Confirm appointment' })
const retryButton = () => screen.findByRole('button', { name: 'Try again' })
const reasonField = () => screen.getByRole('textbox', { name: 'Reason for visit (optional)' })
const live = () => screen.getByRole('status')
const booked = () => screen.queryByRole('heading', { name: 'Appointment booked' })
const posts = (api: ReturnType<typeof serve>) => api.sent.filter((request) => (request.method ?? 'get').toLowerCase() !== 'get')

/** A claim that something was booked: "booked", "confirmed", or "reserved" not preceded by "not". */
const CLAIMS_BOOKED = /\b(booked|confirmed|booking confirmed|appointment (is )?(set|made))\b|(?<!not )\breserved\b/i

/** A request that got no answer in time, as Axios reports one. */
const timedOut: ScriptedAnswer = (config) => {
  throw new AxiosError('timeout of 30000ms exceeded', 'ECONNABORTED', config)
}

describe('booking — reviewing the chosen slot', () => {
  it('shows the hospital, the doctor and the slot on the hospital’s clock, claims nothing, and sends nothing', async () => {
    const { api, booking } = setup()
    open()

    expect(await title('Booking')).toBeInTheDocument()
    expect(document.title).toBe('Booking · Atheris Health')

    const review = screen.getByRole('region', { name: 'Review your appointment' })
    expect(within(review).getByText('Hospital').nextElementSibling).toHaveTextContent(/^City Care Hospital$/)
    expect(within(review).getByText('Doctor').nextElementSibling).toHaveTextContent(/^Asha RaoInterventional Cardiology$/)
    expect(within(review).getByText('Day').nextElementSibling).toHaveTextContent(/^Friday 9 October 2026$/)
    expect(within(review).getByText('Time').nextElementSibling).toHaveTextContent(/^10:15 – 10:30$/)
    expect(within(review).getByText('Times are in the hospital’s local time (Asia/Kolkata)')).toBeInTheDocument()

    expect(
      screen.getByText(
        'This app is not for emergencies. In an emergency, call your local emergency number or go to the nearest emergency department.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText('This time is not held for you. It can still be taken until you confirm and the hospital accepts it.')).toBeInTheDocument()
    expect(reasonField()).toHaveValue('')
    expect(screen.getByText('0 of 500 characters')).toBeInTheDocument()
    expect(await confirmButton()).toBeEnabled()

    // Nothing is claimed, nothing is announced, and nothing is an alert.
    expect(main()).not.toHaveTextContent(CLAIMS_BOOKED)
    expect(live()).toBeEmptyDOMElement()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    // Both ways back lead to the availability as it was left.
    const back = `${AVAILABILITY_PATH}?date=2026-10-09&slot=${q(START)}`
    expect(screen.getByRole('link', { name: 'Back to availability' })).toHaveAttribute('href', back)
    expect(screen.getByRole('link', { name: 'Choose another time' })).toHaveAttribute('href', back)

    // The hospital and the doctor are all that is read. Looking at the review books, holds and reserves nothing.
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA])
    expect(booking.requests).toHaveLength(0)
  })

  it('shows the time on the HOSPITAL’s clock, not the browser’s: a zone fourteen hours ahead of UTC', async () => {
    const kiritimati = { ...cityCareHospital, timezone: 'Pacific/Kiritimati' }
    const booking = bookingEndpoint(kiritimati, ashaRao)
    serve({ ...cityCareWith(), [HOSPITAL]: ok(kiritimati), ...booking.routes })
    // 22:00Z on the 8th is noon on the 9th in Kiritimati.
    const { user } = open(`date=2026-10-09&start=${q('2026-10-08T22:00:00Z')}&end=${q('2026-10-08T22:15:00Z')}`)

    await title('Booking')
    expect(screen.getByText('Friday 9 October 2026')).toBeInTheDocument()
    expect(screen.getByText('12:00 – 12:15')).toBeInTheDocument()
    expect(screen.getByText('Times are in the hospital’s local time (Pacific/Kiritimati)')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/22:00|22:15|Thursday/)

    await user.click(await confirmButton())

    // The confirmation is on the same clock, and names it.
    expect(await title('Appointment booked')).toBeInTheDocument()
    const details = screen.getByRole('region', { name: 'Appointment details' })
    expect(within(details).getByText('Day').nextElementSibling).toHaveTextContent(/^Friday 9 October 2026$/)
    expect(within(details).getByText('Time').nextElementSibling).toHaveTextContent(/^12:00 – 12:15$/)
    expect(within(details).getByText('Times are in the hospital’s local time (Pacific/Kiritimati)')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/22:00|22:15|Thursday/)
    // The instants went out exactly as the link carried them.
    expect(booking.requests[0].body).toEqual({ start: '2026-10-08T22:00:00Z', end: '2026-10-08T22:15:00Z', type: 'new' })
  })

  it.each([
    ['nothing at all', ''],
    ['no date', `start=${q(START)}&end=${q(END)}`],
    ['a date that is not one', `date=2026-02-30&start=${q(START)}&end=${q(END)}`],
    ['a date with a time', `date=2026-10-09T00:00&start=${q(START)}&end=${q(END)}`],
    ['no start', `date=2026-10-09&end=${q(END)}`],
    ['a start without an offset', `date=2026-10-09&start=${q('2026-10-09T10:15:00')}&end=${q(END)}`],
    ['a start that is not a time', `date=2026-10-09&start=quarter+past+ten&end=${q(END)}`],
    ['a start with its plus left bare', `date=2026-10-09&start=2026-10-09T10:15:00+05:30&end=${q(END)}`],
    ['no end', `date=2026-10-09&start=${q(START)}`],
    ['an end without an offset', `date=2026-10-09&start=${q(START)}&end=${q('2026-10-09T10:30:00')}`],
    ['an end before the start', `date=2026-10-09&start=${q(END)}&end=${q(START)}`],
    ['an end at the start', `date=2026-10-09&start=${q(START)}&end=${q(START)}`],
    ['a start on another day', `date=2026-10-08&start=${q(START)}&end=${q(END)}`],
    ['a start on another day by the hospital’s clock', `date=2026-10-08&start=${q('2026-10-08T20:00:00Z')}&end=${q('2026-10-08T20:15:00Z')}`],
    ['markup in the date', `date=${q('<img src=x onerror=window.pwned=1>')}&start=${q(START)}&end=${q(END)}`],
    ['markup in the start', `date=2026-10-09&start=${q('<script>window.pwned=1</script>')}&end=${q(END)}`],
  ])('ATTACK — a link with %s is not a valid link: nothing of it is shown, nothing can be confirmed, nothing is sent', async (_case, query) => {
    const { api, booking } = setup()
    open(query)

    expect(await title('Booking')).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'This booking link is not valid' })).toBeInTheDocument()
    expect(screen.getByText('Choose a day and a time from the doctor’s availability to continue.')).toBeInTheDocument()
    expect(screen.queryByText('Review your appointment')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(CLAIMS_BOOKED)
    // Nothing from the address is echoed: no date, no time, no markup.
    expect(main()).not.toHaveTextContent(/2026|10:15|10:30|20:00|October|Friday|Thursday|quarter/)
    expect(main().querySelector('script, img, [onerror]')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
    // There is nothing to confirm with: no form, no field, no button.
    expect(main().querySelector('form, textarea, input, button')).toBeNull()
    expect(screen.getByRole('link', { name: 'Go to availability' })).toHaveAttribute('href', AVAILABILITY_PATH)
    expect(screen.getByRole('link', { name: 'Back to availability' })).toHaveAttribute('href', AVAILABILITY_PATH)
    expect(screen.getByRole('link', { name: 'Back to the profile' })).toHaveAttribute('href', ASHA_PATH)
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA])
    expect(booking.requests).toHaveLength(0)
  })

  it('a hospital whose zone the browser does not know cannot vouch for the day: the link is not valid', async () => {
    const { booking } = setup({}, { [HOSPITAL]: ok({ ...cityCareHospital, timezone: 'Mars/Olympus' }) })
    open()

    expect(await title('Booking')).toBeInTheDocument()
    expect(screen.getByText('This booking link is not valid')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/10:15|Friday/)
    expect(screen.queryByRole('button', { name: 'Confirm appointment' })).not.toBeInTheDocument()
    expect(booking.requests).toHaveLength(0)
  })

  it('NOT FOUND — an unknown or hidden doctor is the doctor’s own page, whatever the link says', async () => {
    const { api, booking } = setup({}, { [ASHA]: fail(404, 'RESOURCE_NOT_FOUND', 'server wording') })
    open()

    expect(await title('This doctor is not available')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse doctors' })).toHaveAttribute('href', DOCTORS_PATH)
    expect(document.body).not.toHaveTextContent(/server wording|Friday|10:15|Booking|Confirm/)
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA])
    expect(booking.requests).toHaveLength(0)
  })

  it.each([
    ['a path climb', `${DOCTORS_PATH}/..%2F..%2Fme/book?${SOUND}`, [HOSPITAL]],
    ['a doctor reference that is not UUID-shaped', `${DOCTORS_PATH}/asha-rao/book?${SOUND}`, [HOSPITAL]],
    ['a hospital reference with a dot', `/hospitals/city.care/doctors/${ashaRao.ref}/book?${SOUND}`, []],
  ])('ATTACK — %s is never sent: not as a read, not as a booking', async (_case, path, sent) => {
    const api = serve({ [HOSPITAL]: ok(cityCareHospital) })
    signIn('access-1')
    renderApp(path)

    expect(await title(/^This (doctor|hospital) is not available$/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm appointment' })).not.toBeInTheDocument()
    expect(api.sent.map(routeOf)).toEqual(sent)
  })
})

describe('booking — confirming', () => {
  it('sends ONE request: the slot and "new" in the body, the key in a header, no patient, hospital or doctor in either', async () => {
    const { api, booking } = setup()
    const { user, router } = open()

    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toHaveFocus()
    expect(posts(api).map(routeOf)).toEqual([`POST /hospitals/city-care/doctors/${ashaRao.ref}/appointments`])
    expect(booking.requests).toHaveLength(1)
    const [request] = booking.requests
    expect(request.body).toEqual({ start: START, end: END, type: 'new' })
    expect(request.key).toMatch(KEY)
    expect(request.authorization).toBe('Bearer access-1')
    // The server works out who is booking, where and with whom from the session and the path.
    expect(request.rawBody).not.toMatch(/patient|hospital|doctor|account|phone|mrn|status|_id|"id"/i)
    expect(Object.keys(api.sent.at(-1)?.params ?? {})).toEqual([])

    // The confirmation is the server's answer.
    expect(document.title).toBe('Appointment booked · Atheris Health')
    expect(live()).toHaveTextContent(/^Appointment booked$/)
    const details = screen.getByRole('region', { name: 'Appointment details' })
    const reference = within(details).getByText('Reference').nextElementSibling
    expect(reference).toHaveTextContent(new RegExp(`^${appointmentRef(1)}$`))
    expect(reference?.className).toMatch(/font-mono/)
    expect(reference?.className).toMatch(/select-all/)
    expect(within(details).getByText('Status').nextElementSibling).toHaveTextContent(/^Booked$/)
    expect(within(details).getByText('Doctor').nextElementSibling).toHaveTextContent(/^Asha RaoInterventional Cardiology$/)
    expect(within(details).getByText('Hospital').nextElementSibling).toHaveTextContent(/^City Care Hospital$/)
    expect(within(details).getByText('Day').nextElementSibling).toHaveTextContent(/^Friday 9 October 2026$/)
    expect(within(details).getByText('Time').nextElementSibling).toHaveTextContent(/^10:15 – 10:30$/)
    expect(within(details).getByText('Times are in the hospital’s local time (Asia/Kolkata)')).toBeInTheDocument()

    // The reference is the way to the appointment's own page, and the list of them is the first way on.
    expect(within(details).getByRole('link', { name: appointmentRef(1) })).toHaveAttribute('href', `/appointments/${appointmentRef(1)}`)
    const waysOn = [...main().querySelectorAll('a')].filter((link) => !details.contains(link))
    expect(waysOn.map((link) => [link.textContent, link.getAttribute('href')])).toEqual([
      ['View my appointments', '/appointments'],
      ['Back to home', '/'],
      ['Back to the doctor', ASHA_PATH],
    ])
    expect(waysOn[0]).toHaveAttribute('data-variant', 'default')

    // Nothing is left to confirm with, and the spent slot is out of the address.
    expect(main().querySelector('form, textarea, button')).toBeNull()
    expect(router.state.location.pathname).toBe(BOOK_PATH)
    expect(router.state.location.search).toBe('')
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(booking.appointments).toHaveLength(1)
  })

  it('sends the reason trimmed when one is typed, as text', async () => {
    const { booking } = setup()
    const { user } = open()

    await confirmButton()
    await user.type(reasonField(), '   Chest pain on the stairs, two weeks.  ')
    expect(screen.getByText('36 of 500 characters')).toBeInTheDocument()
    await user.click(await confirmButton())

    await title('Appointment booked')
    expect(booking.requests[0].body).toEqual({ start: START, end: END, type: 'new', reason: 'Chest pain on the stairs, two weeks.' })
    // The reason is not echoed on the confirmation, by the page or from the response.
    expect(main()).not.toHaveTextContent(/Chest pain/)
  })

  it.each([
    ['left empty', ''],
    ['only spaces and new lines', '   \n\n  '],
  ])('leaves the reason out of the body when it is %s', async (_case, text) => {
    const { booking } = setup()
    const { user } = open()
    await confirmButton()

    if (text) fireEvent.change(reasonField(), { target: { value: text } })
    expect(screen.getByText('0 of 500 characters')).toBeInTheDocument()
    await user.click(await confirmButton())

    await title('Appointment booked')
    expect(booking.requests[0].rawBody).toBe(JSON.stringify({ start: START, end: END, type: 'new' }))
  })

  it('a reason over 500 characters is stopped on the page: nothing is sent until it fits', async () => {
    const { booking } = setup()
    const { user } = open()
    await confirmButton()

    fireEvent.change(reasonField(), { target: { value: 'a'.repeat(501) } })
    expect(screen.getByText('501 of 500 characters')).toBeInTheDocument()
    await user.click(await confirmButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/^Keep the reason to 500 characters or fewer\.$/)
    expect(reasonField()).toHaveFocus()
    expect(reasonField()).toHaveAttribute('aria-invalid', 'true')
    expect(reasonField()).toHaveAccessibleDescription(/Keep the reason to 500 characters or fewer\./)
    expect(booking.requests).toHaveLength(0)
    expect(booked()).not.toBeInTheDocument()

    // Exactly 500 fits; space around it is not counted, and is not sent.
    fireEvent.change(reasonField(), { target: { value: `  ${'a'.repeat(500)}\n` } })
    expect(screen.getByText('500 of 500 characters')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    await user.click(await confirmButton())

    await title('Appointment booked')
    expect(booking.requests).toHaveLength(1)
    expect(booking.requests[0].body).toEqual({ start: START, end: END, type: 'new', reason: 'a'.repeat(500) })
  })

  it('says "booked" only AFTER the server has: while the request is out, the page is busy and claims nothing', async () => {
    const { booking } = setup()
    const answer = deferred()
    booking.next(answer.handler)
    const { user } = open()

    const button = await confirmButton()
    await user.click(button)

    await waitFor(() => expect(button).toBeDisabled())
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(button.closest('form')).toHaveAttribute('aria-busy', 'true')
    expect(live()).toHaveTextContent(/^Booking your appointment…$/)
    expect(reasonField()).toHaveAttribute('readonly')
    // No way off the page is offered as a button to press mid-request, and nothing is claimed.
    expect(screen.getByRole('button', { name: 'Choose another time' })).toBeDisabled()
    expect(booked()).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(CLAIMS_BOOKED)
    expect(main()).not.toHaveTextContent(appointmentRef(1))
    expect(booking.requests).toHaveLength(1)

    await act(async () => answer.answer(ok(appointmentOf(cityCareHospital, ashaRao, START, END), 201)))

    expect(await title('Appointment booked')).toHaveFocus()
    expect(booking.requests).toHaveLength(1)
  })

  it.each([
    ['created (201)', 201],
    ['replayed (200)', 200],
  ])('shows the confirmation with its reference for an answer that is %s', async (_case, status) => {
    const { booking } = setup()
    const ref = 'b1111111-2222-4333-8444-555555555555'
    booking.next(ok(appointmentOf(cityCareHospital, ashaRao, START, END, { ref }), status))
    const { user } = open()

    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(screen.getByText(ref)).toBeInTheDocument()
    expect(booking.requests).toHaveLength(1)
  })

  it.each([
    ['a double click', async (button: HTMLElement, user: ReturnType<typeof open>['user']) => user.dblClick(button)],
    [
      'Enter and then a click',
      async (button: HTMLElement, user: ReturnType<typeof open>['user']) => {
        button.focus()
        await user.keyboard('{Enter}')
        await user.click(button)
      },
    ],
    [
      'ten rapid clicks',
      async (button: HTMLElement) => {
        // Raw events, all before the page can redraw: `disabled` has not arrived to stop any of them.
        act(() => {
          for (let click = 0; click < 10; click++) fireEvent.click(button)
        })
      },
    ],
    [
      'ten submissions of the form itself',
      async (button: HTMLElement) => {
        act(() => {
          for (let submission = 0; submission < 10; submission++) fireEvent.submit(button.closest('form')!)
        })
      },
    ],
  ])('ATTACK — %s books once: one request leaves, whatever the button looked like', async (_case, press) => {
    const { booking } = setup()
    const answer = deferred()
    booking.next(answer.handler)
    const { user } = open()
    const button = await confirmButton()

    await press(button, user)
    await waitFor(() => expect(booking.requests).toHaveLength(1))
    // Still out, and still pressed at.
    act(() => {
      fireEvent.click(button)
      fireEvent.submit(button.closest('form')!)
    })
    await act(async () => answer.answer(ok(appointmentOf(cityCareHospital, ashaRao, START, END), 201)))

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(booking.requests).toHaveLength(1)
  })

  it('an answer that arrives after the patient has left the page changes nothing where they are now', async () => {
    const { booking } = setup()
    const answer = deferred()
    booking.next(answer.handler)
    const { user, router } = open()
    await user.click(await confirmButton())
    await waitFor(() => expect(booking.requests).toHaveLength(1))

    await act(() => router.navigate(ASHA_PATH))
    expect(await title('Asha Rao')).toBeInTheDocument()
    await act(async () => answer.answer(ok(appointmentOf(cityCareHospital, ashaRao, START, END), 201)))

    expect(router.state.location.pathname).toBe(ASHA_PATH)
    expect(booked()).not.toBeInTheDocument()
    expect(booking.requests).toHaveLength(1)
  })
})

describe('booking — refusals: the page says what happened, stays where it is, and picks nothing itself', () => {
  const DATE_ONLY = `${AVAILABILITY_PATH}?date=2026-10-09`
  const WITH_SLOT = `${AVAILABILITY_PATH}?date=2026-10-09&slot=${q(START)}`

  it.each([
    ['the slot was taken (409)', refusals.slotTaken(), 'This time is no longer available.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['the key was used for another request (409)', refusals.keyReused(), 'This time is no longer available.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['a conflict the app does not know (409)', fail(409, 'SOMETHING_NEW', 'server wording'), 'This time is no longer available.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['the slot cannot be booked (400)', refusals.notBookable(), 'This time can no longer be booked.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['a rule the app does not know (400)', fail(400, 'BUSINESS_RULE_VIOLATION', 'server wording'), 'This time can no longer be booked.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['a 400 with no envelope at all', { status: 400, data: '<html>server wording</html>' }, 'This time can no longer be booked.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['the patient’s own appointment overlaps (400)', refusals.ownOverlap(), 'You already have an appointment at this time.', 'Choose another time', DATE_ONLY, DATE_ONLY],
    ['the limit of upcoming appointments (400)', refusals.limitReached(), 'You have reached the limit of upcoming appointments at this hospital.', 'Back to the doctor', ASHA_PATH, WITH_SLOT],
    ['no record link at the hospital (403)', refusals.linkRequired(), 'Link your record at this hospital before booking.', 'Link my record', '/link-patient', WITH_SLOT],
  ])('%s', async (_case, answer, message, action, href, back) => {
    const { api, booking } = setup()
    booking.next(answer)
    const { user, router } = open()
    const before = router.state.location

    await user.click(await confirmButton())

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(new RegExp(`^${message.replace(/[.]/g, '\\.')}$`))
    await waitFor(() => expect(alert).toHaveFocus())
    expect(screen.getByRole('link', { name: action })).toHaveAttribute('href', href)
    // A slot that was refused is not carried back to the availability.
    for (const link of screen.getAllByRole('link', { name: 'Back to availability' })) expect(link).toHaveAttribute('href', back)
    for (const link of screen.getAllByRole('link')) {
      if (href === DATE_ONLY) expect(link.getAttribute('href')).not.toContain('slot=')
    }

    // The page is where it was, on the slot the patient chose; nothing else was tried, and nothing is claimed.
    expect(router.state.location).toBe(before)
    expect(await title('Booking')).toBeInTheDocument()
    expect(screen.getByText('10:15 – 10:30')).toBeInTheDocument()
    expect(booked()).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/server wording|Appointment booked|Reference/)
    expect(live()).toBeEmptyDOMElement()
    // Refused is refused: this slot cannot be confirmed again from here.
    expect(main().querySelector('form, textarea, button')).toBeNull()
    expect(booking.requests).toHaveLength(1)
    expect(posts(api)).toHaveLength(1)
    expect(api.sent.map(routeOf).filter((route) => route.includes('availability'))).toEqual([])
    expect(booking.appointments).toHaveLength(0)
  })

  it('"Link my record" opens the link form with this hospital filled in, as the hospital’s own page does', async () => {
    const { booking } = setup()
    booking.next(refusals.linkRequired())
    const { user } = open()
    await user.click(await confirmButton())

    await user.click(await screen.findByRole('link', { name: 'Link my record' }))

    expect(await screen.findByLabelText('Hospital code')).toHaveValue('city-care')
  })

  it('NOT FOUND (404) — the doctor’s "not available" page, in place of the review', async () => {
    const { booking } = setup()
    booking.next(fail(404, 'RESOURCE_NOT_FOUND', 'server wording'))
    const { user } = open()

    await user.click(await confirmButton())

    expect(await title('This doctor is not available')).toHaveFocus()
    expect(screen.getByRole('link', { name: 'Browse doctors' })).toHaveAttribute('href', DOCTORS_PATH)
    expect(main()).not.toHaveTextContent(/server wording|Friday|10:15|Asha|booked/)
    expect(main().querySelector('form, textarea, button')).toBeNull()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(booking.requests).toHaveLength(1)
  })

  it('INVALID (422) — the request was not one the server could read: the link is not valid, and the way back is offered', async () => {
    const { booking } = setup()
    booking.next(fail(422, 'VALIDATION_ERROR', 'server wording'))
    const { user } = open()

    await user.click(await confirmButton())

    const heading = await screen.findByRole('heading', { level: 2, name: 'This booking link is not valid' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(screen.getByRole('link', { name: 'Go to availability' })).toHaveAttribute('href', AVAILABILITY_PATH)
    expect(screen.getByRole('link', { name: 'Back to availability' })).toHaveAttribute('href', AVAILABILITY_PATH)
    expect(main()).not.toHaveTextContent(/server wording|Friday|10:15|booked/)
    expect(main().querySelector('form, textarea, button')).toBeNull()
    expect(booking.requests).toHaveLength(1)
  })

  it('a slot the server will not book is refused by the endpoint’s own rule, not by a script', async () => {
    const { booking } = setup({ bookable: () => false })
    const { user } = open()

    await user.click(await confirmButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/^This time can no longer be booked\.$/)
    expect(booking.appointments).toHaveLength(0)
  })

  it('a slot someone else booked a moment ago is "no longer available"', async () => {
    const { booking } = setup()
    // Another patient, another key, the same slot.
    signIn('access-other')
    await client.post(booking.route.replace('POST ', ''), { start: START, end: END }, { headers: { 'Idempotency-Key': 'another-patients-key-0001' } })
    expect(booking.appointments).toHaveLength(1)

    const { user } = open()
    await user.click(await confirmButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/^This time is no longer available\.$/)
    expect(booking.appointments).toHaveLength(1)
  })
})

describe('booking — an answer that did not arrive: the SAME request is sent again', () => {
  const malformed = (overrides: Record<string, unknown>) => ok(appointmentOf(cityCareHospital, ashaRao, START, END, overrides), 201)

  it.each<[string, ScriptedAnswer, string, string]>([
    ['no connection', unreachable, 'No connection', 'We could not reach the server. Check your internet connection and try again — you will not be booked twice.'],
    ['a request that timed out', timedOut, 'No connection', 'We could not reach the server. Check your internet connection and try again — you will not be booked twice.'],
    ['a 503', fail(503, 'SERVICE_UNAVAILABLE', 'server wording'), 'We could not book the appointment', 'Something went wrong on our side. Try again — you will not be booked twice.'],
    ['a 500 with no envelope', { status: 500, data: '<html>server wording</html>' }, 'We could not book the appointment', 'Something went wrong on our side. Try again — you will not be booked twice.'],
    ['a 429', fail(429, 'RATE_LIMITED', 'server wording'), 'We could not book the appointment', 'Something went wrong on our side. Try again — you will not be booked twice.'],
    ['a pending policy (403)', refusals.consentRequired(), 'We could not book the appointment', 'We cannot book this in the app right now. Please try again later.'],
    ['a 2xx with no body', noContent(), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx with no appointment in it', ok(null, 201), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx without a reference', malformed({ ref: undefined }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx whose reference is not one', malformed({ ref: '<img src=x onerror=window.pwned=1>' }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx without a status', malformed({ status: undefined }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx in a state that is not "booked"', malformed({ status: 'cancelled' }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx whose start has no offset', malformed({ start: '2026-10-09T10:15:00' }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx whose end is not after its start', malformed({ end: START }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx without a time zone', malformed({ timezone: '' }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx without the hospital’s name', malformed({ hospital: { ref: 'city-care' } }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
    ['a 2xx without the doctor’s name', malformed({ doctor: { ref: ashaRao.ref, name: '  ' } }), 'We could not confirm the booking', 'Try again — you will not be booked twice.'],
  ])('%s: nothing is claimed, and "Try again" sends the same key and the same body', async (_case, answer, heading, body) => {
    const { booking } = setup()
    booking.next(answer)
    const { user, router } = open()
    const before = router.state.location

    await confirmButton()
    await user.type(reasonField(), 'Follow-up on the ECG')
    await user.click(await confirmButton())

    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText(heading)).toBeInTheDocument()
    expect(within(alert).getByText(body)).toBeInTheDocument()
    await waitFor(() => expect(alert).toHaveFocus())
    expect(booked()).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/server wording|Appointment booked|Reference/)
    expect(main().querySelector('img, script')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
    expect(live()).toBeEmptyDOMElement()
    expect(router.state.location).toBe(before)
    // The review is still there, and so is the way to another time — with the slot, which was not refused.
    expect(screen.getByText('10:15 – 10:30')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Choose another time' })).toHaveAttribute('href', `${AVAILABILITY_PATH}?date=2026-10-09&slot=${q(START)}`)
    expect(screen.queryByRole('button', { name: 'Confirm appointment' })).not.toBeInTheDocument()
    // The request that went out is the request: its reason can no longer be edited into another one.
    expect(reasonField()).toHaveAttribute('readonly')
    expect(reasonField()).toHaveAccessibleDescription(/The reason cannot be changed while this request is being tried again\./)
    expect(booking.requests).toHaveLength(1)

    await user.click(await retryButton())

    expect(await title('Appointment booked')).toHaveFocus()
    expect(booking.requests).toHaveLength(2)
    const [first, second] = booking.requests
    expect(first.key).toMatch(KEY)
    expect(second.key).toBe(first.key)
    expect(second.rawBody).toBe(first.rawBody)
    expect(second.body).toEqual({ start: START, end: END, type: 'new', reason: 'Follow-up on the ECG' })
    expect(booking.appointments).toHaveLength(1)
  })

  it('a booking the server made but whose answer was lost is not made twice: the retry gets the same appointment back', async () => {
    const { booking } = setup()
    booking.next(LOST)
    const { user } = open()

    await user.click(await confirmButton())

    expect(await screen.findByText('No connection')).toBeInTheDocument()
    // It exists on the server, and the page does not know — so it does not say.
    expect(booking.appointments).toHaveLength(1)
    expect(booked()).not.toBeInTheDocument()

    await user.click(await retryButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(screen.getByText(appointmentRef(1))).toBeInTheDocument()
    expect(booking.appointments).toHaveLength(1)
    expect(booking.requests).toHaveLength(2)
    expect(booking.requests[1].key).toBe(booking.requests[0].key)
  })

  it('keeps the same key through any number of failures, each announced afresh', async () => {
    const { booking } = setup()
    booking.next(unreachable, fail(503, 'SERVICE_UNAVAILABLE'), ok({ ref: 'not-an-appointment' }, 201), timedOut)
    const { user } = open()

    await user.click(await confirmButton())
    expect(await screen.findByText('No connection')).toBeInTheDocument()
    await user.click(await retryButton())
    expect(await screen.findByText('We could not book the appointment')).toBeInTheDocument()
    await user.click(await retryButton())
    expect(await screen.findByText('We could not confirm the booking')).toBeInTheDocument()
    await user.click(await retryButton())
    const alert = await screen.findByRole('alert')
    expect(within(alert).getByText('No connection')).toBeInTheDocument()
    await waitFor(() => expect(alert).toHaveFocus())
    await user.click(await retryButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(booking.requests).toHaveLength(5)
    expect(new Set(booking.requests.map((request) => request.key)).size).toBe(1)
    expect(new Set(booking.requests.map((request) => request.rawBody)).size).toBe(1)
  })

  it('a retry that is refused ends there: the refusal is shown and there is nothing more to press', async () => {
    const { booking } = setup()
    booking.next(unreachable, refusals.slotTaken())
    const { user } = open()

    await user.click(await confirmButton())
    await user.click(await retryButton())

    expect(await screen.findByText('This time is no longer available.')).toBeInTheDocument()
    expect(main().querySelector('form, textarea, button')).toBeNull()
    expect(booking.requests).toHaveLength(2)
  })
})

describe('booking — the idempotency key', () => {
  const keyOf = (booking: ReturnType<typeof bookingEndpoint>) => booking.requests.at(-1)?.key ?? ''

  it('is new for every review: opening the page again gives another key', async () => {
    const { booking } = setup()
    booking.next(unreachable, unreachable)
    const first = open()
    await first.user.click(await confirmButton())
    await screen.findByText('No connection')
    const firstKey = keyOf(booking)

    cleanup()
    const second = open()
    await second.user.click(await confirmButton())
    await screen.findByText('No connection')

    expect(booking.requests).toHaveLength(2)
    expect(firstKey).toMatch(KEY)
    expect(keyOf(booking)).toMatch(KEY)
    expect(keyOf(booking)).not.toBe(firstKey)
  })

  it('is new for another slot: the key of one slot is never sent with another', async () => {
    const { booking } = setup()
    booking.next(unreachable)
    const { user, router } = open()
    await user.click(await confirmButton())
    await screen.findByText('No connection')
    const firstKey = keyOf(booking)

    await act(() => router.navigate(`${BOOK_PATH}?${OTHER_SLOT}`))
    expect(await screen.findByText('10:00 – 10:15')).toBeInTheDocument()
    // A fresh review: nothing of the last attempt is carried over.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(reasonField()).not.toHaveAttribute('readonly')
    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(booking.requests).toHaveLength(2)
    expect(keyOf(booking)).not.toBe(firstKey)
    expect(booking.requests[1].body).toEqual({ start: '2026-10-09T10:00:00+05:30', end: START, type: 'new' })
  })

  it('an expired session is refreshed and the SAME request is sent again: same key, same body, new token', async () => {
    const { api, booking } = setup({}, { [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }) })
    booking.next(fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'))
    const { user } = open()

    await confirmButton()
    await user.type(reasonField(), 'Annual review')
    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(api.calls(REFRESH)).toHaveLength(1)
    expect(booking.requests).toHaveLength(2)
    const [refused, retried] = booking.requests
    expect(refused.authorization).toBe('Bearer access-1')
    expect(retried.authorization).toBe('Bearer access-new')
    expect(retried.key).toBe(refused.key)
    expect(retried.key).toMatch(KEY)
    expect(retried.rawBody).toBe(refused.rawBody)
    expect(booking.appointments).toHaveLength(1)
    // The patient saw one confirmation, and no failure on the way to it.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('server wording')
  })

  it('a session that cannot be refreshed ends at the sign-in page, with nothing claimed', async () => {
    const { booking } = setup({}, { [REFRESH]: fail(401, 'UNAUTHORIZED') })
    booking.next(fail(401, 'AUTHENTICATION_REQUIRED'))
    const { user, router } = open()

    await user.click(await confirmButton())

    expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(booking.requests).toHaveLength(1)
    expect(document.body).not.toHaveTextContent(/Appointment booked/)
  })

  it('ATTACK — is found nowhere a script or a bystander could read it: not the address, not the page, not storage', async () => {
    const { booking } = setup()
    booking.next(unreachable)
    const { user, router } = open()
    const everywhere = () =>
      [
        document.documentElement.outerHTML,
        document.title,
        document.cookie,
        JSON.stringify(router.state.location),
        JSON.stringify(window.history.state),
        window.location.href,
        JSON.stringify({ ...window.localStorage }),
        JSON.stringify({ ...window.sessionStorage }),
      ].join('\n')

    await user.click(await confirmButton())
    await screen.findByText('No connection')
    const key = keyOf(booking)
    expect(key).toMatch(KEY)
    expect(everywhere()).not.toContain(key)

    await user.click(await retryButton())
    await title('Appointment booked')

    expect(everywhere()).not.toContain(key)
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
    // It was in the one place it belongs: the header of the two requests.
    expect(booking.requests.map((request) => request.key)).toEqual([key, key])
    expect(booking.requests.every((request) => !request.rawBody.includes(key))).toBe(true)
  })
})

describe('booking — what a response and a name may put on the screen', () => {
  it('ATTACK — fields the contract does not name never reach the confirmation', async () => {
    const { booking } = setup()
    booking.next(
      ok(
        {
          ...appointmentOf(cityCareHospital, ashaRao, START, END),
          id: 'INTERNAL-ID-SECRET',
          patient_id: 'PATIENT-SECRET',
          hospital_id: 'HOSPITAL-ID-SECRET',
          notes: 'NOTES-SECRET',
          created_by: 'STAFF-SECRET',
          fee: 'FEE-999',
          reason: 'REASON-ECHO-SECRET',
          type: 'TYPE-SECRET',
          hospital: { ref: 'REF-SECRET', name: 'City Care Hospital', hospital_id: 'NESTED-HOSPITAL-SECRET', phone: 'PHONE-SECRET' },
          doctor: { ref: 'DREF-SECRET', name: 'Asha Rao', specialization: 'Interventional Cardiology', user_id: 'USER-SECRET', fee: 'FEE-998' },
        },
        201,
      ),
    )
    const { user } = open()

    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(document.documentElement.outerHTML).not.toMatch(/SECRET|FEE-99/)
    expect(screen.getByText(appointmentRef(1))).toBeInTheDocument()
  })

  it('copies exactly the fields the page shows, and nothing else', () => {
    const smuggled = { ...appointmentOf(cityCareHospital, ashaRao, START, END), patient_id: 'p', notes: 'n', created_by: 'c', fee: 1 }

    expect(toBookedAppointment(smuggled)).toEqual({
      ref: appointmentRef(1),
      status: 'booked',
      start: START,
      end: END,
      timezone: 'Asia/Kolkata',
      hospital: { name: 'City Care Hospital' },
      doctor: { name: 'Asha Rao', specialization: 'Interventional Cardiology' },
    })
  })

  it('ATTACK — markup in a name or in the reason is text: shown as written, sent as written, never run', async () => {
    const hostileHospital = { ...cityCareHospital, name: '<img src=x onerror=window.pwned=1>' }
    const hostileDoctor = { ...ashaRao, name: '<script>window.pwned=1</script>', specialization: '<b onmouseover=window.pwned=1>Cardiology</b>' }
    const booking = bookingEndpoint(hostileHospital, hostileDoctor)
    serve({ ...cityCareWith(), [HOSPITAL]: ok(hostileHospital), [ASHA]: ok(hostileDoctor), ...booking.routes })
    const { user } = open()
    await confirmButton()
    const clean = () => {
      expect(main().querySelector('script, img, b, [onerror], [onmouseover]')).toBeNull()
      expect((window as { pwned?: unknown }).pwned).toBeUndefined()
    }

    expect(screen.getByText('<img src=x onerror=window.pwned=1>')).toBeInTheDocument()
    expect(screen.getByText(/^<script>window\.pwned=1<\/script>/)).toBeInTheDocument()
    expect(screen.getByText('<b onmouseover=window.pwned=1>Cardiology</b>')).toBeInTheDocument()
    clean()

    const reason = '<img src=x onerror=window.pwned=1> "quoted" & <script>window.pwned=1</script>'
    fireEvent.change(reasonField(), { target: { value: reason } })
    clean()
    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(booking.requests[0].body).toEqual({ start: START, end: END, type: 'new', reason })
    expect(screen.getByText('<img src=x onerror=window.pwned=1>')).toBeInTheDocument()
    expect(screen.getByText(/^<script>window\.pwned=1<\/script>/)).toBeInTheDocument()
    clean()
  })

  it('ATTACK — a hostile time zone in the answer is text too, and the times are still the server’s own', async () => {
    const { booking } = setup()
    booking.next(ok(appointmentOf(cityCareHospital, ashaRao, START, END, { timezone: '<img src=x onerror=window.pwned=1>' }), 201))
    const { user } = open()

    await user.click(await confirmButton())

    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(screen.getByText('Times are in the hospital’s local time (<img src=x onerror=window.pwned=1>)')).toBeInTheDocument()
    // Read off the offset the server wrote, never off the browser's clock.
    expect(screen.getByText('10:15 – 10:30')).toBeInTheDocument()
    expect(screen.getByText('Friday 9 October 2026')).toBeInTheDocument()
    expect(main().querySelector('img')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
  })
})

describe('booking — after it is booked', () => {
  it('Back and Forward do not book again: the entry no longer names the slot, and nothing can be confirmed from it', async () => {
    const { api, booking } = setup()
    signIn('access-1')
    const { user, router } = renderApp(`${AVAILABILITY_PATH}?date=2026-10-09&slot=${q(START)}`)
    await user.click(await screen.findByRole('link', { name: 'Continue to booking' }))
    await user.click(await confirmButton())
    expect(await title('Appointment booked')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /confirm|try again/i })).not.toBeInTheDocument()

    await act(() => router.navigate(-1))
    expect(await title('Availability')).toBeInTheDocument()
    expect(booking.requests).toHaveLength(1)

    await act(() => router.navigate(1))
    expect(await title('Booking')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe(BOOK_PATH)
    expect(router.state.location.search).toBe('')
    expect(screen.getByText('This booking link is not valid')).toBeInTheDocument()
    expect(main().querySelector('form, textarea, button')).toBeNull()

    expect(booking.requests).toHaveLength(1)
    expect(posts(api)).toHaveLength(1)
    expect(booking.appointments).toHaveLength(1)
  })

  it('the availability is read again afterwards: what was on screen before the booking is not trusted', async () => {
    const { api } = setup()
    signIn('access-1')
    const { user, router } = renderApp(`${AVAILABILITY_PATH}?date=2026-10-09&slot=${q(START)}`)
    await user.click(await screen.findByRole('link', { name: 'Continue to booking' }))
    const AVAIL = `${ASHA}/availability`
    const before = api.calls(AVAIL).length
    await user.click(await confirmButton())
    await title('Appointment booked')

    await act(() => router.navigate(-1))
    await title('Availability')

    await waitFor(() => expect(api.calls(AVAIL).length).toBeGreaterThan(before))
  })
})

describe('booking — on to the patient’s appointments', () => {
  /** City Care with Asha Rao's booking endpoint, and the patient's appointments reading what it books. */
  function setupWithAppointments() {
    const booking = bookingEndpoint(cityCareHospital, ashaRao)
    // The slot is on Friday 9 October; "now" is the Wednesday before, as everywhere else.
    const mine = myAppointmentsEndpoints(booking.appointments, { refs: [appointmentRef(1)] })
    const api = serve({ ...cityCareWith(), ...booking.routes, ...mine.routes })
    return { api, booking, mine }
  }

  it('"View my appointments" opens the list, read from the server, with the appointment just booked in it', async () => {
    const { api } = setupWithAppointments()
    const { user, router } = open()
    await user.click(await confirmButton())
    await title('Appointment booked')

    await user.click(screen.getByRole('link', { name: 'View my appointments' }))

    expect(await title('My appointments')).toHaveFocus()
    expect(router.state.location.pathname).toBe('/appointments')
    const card = (await screen.findByRole('heading', { level: 3, name: 'Asha Rao' })).closest('li')!
    expect(within(card).getByText('City Care Hospital')).toBeInTheDocument()
    expect(within(card).getByText('Friday 9 October 2026')).toBeInTheDocument()
    expect(within(card).getByText('10:15 – 10:30 (Asia/Kolkata)')).toBeInTheDocument()
    expect(within(card).getByText('Booked')).toBeInTheDocument()
    expect(api.calls('GET /appointments').map((request) => request.params)).toEqual([{ scope: 'upcoming', page: 1, page_size: 20 }])
  })

  it('a list read BEFORE the booking is not shown again afterwards: the new appointment is asked for', async () => {
    const { api } = setupWithAppointments()
    signIn('access-1')
    const { user, router } = renderApp('/appointments')
    expect(await screen.findByText('When you book an appointment, it will be listed here.')).toBeInTheDocument()

    await act(() => router.navigate(`${BOOK_PATH}?${SOUND}`))
    await user.click(await confirmButton())
    await title('Appointment booked')
    await user.click(screen.getByRole('link', { name: 'View my appointments' }))

    expect(await screen.findByRole('heading', { level: 3, name: 'Asha Rao' })).toBeInTheDocument()
    expect(screen.queryByText('No upcoming appointments')).not.toBeInTheDocument()
    expect(api.calls('GET /appointments')).toHaveLength(2)
  })

  it('the reference opens the appointment’s own page, where the server says whether it can be cancelled', async () => {
    const { mine } = setupWithAppointments()
    const { user, router } = open()
    await user.click(await confirmButton())
    await title('Appointment booked')

    await user.click(screen.getByRole('link', { name: appointmentRef(1) }))

    expect(await title('Appointment')).toHaveFocus()
    expect(router.state.location.pathname).toBe(`/appointments/${appointmentRef(1)}`)
    const details = await screen.findByRole('region', { name: 'Appointment details' })
    expect(within(details).getByText('Status').nextElementSibling).toHaveTextContent(/^Booked$/)
    expect(within(details).getByText('Reference').nextElementSibling).toHaveTextContent(new RegExp(`^${appointmentRef(1)}$`))
    expect(mine.wireOf(appointmentRef(1)).can_cancel).toBe(true)
    expect(within(main()).getByRole('button', { name: 'Cancel appointment' })).toBeEnabled()
    expect(mine.cancelRequests).toHaveLength(0)
  })
})

describe('booking — keyboard and assistive technology', () => {
  it('works from the keyboard alone: availability, review, a reason, confirm, and on from the confirmation', async () => {
    const { booking } = setup()
    signIn('access-1')
    const { user, router } = renderApp(AVAILABILITY_PATH)
    const tabTo = async (target: () => HTMLElement) => {
      for (let presses = 0; presses < 40 && document.activeElement !== target(); presses++) await user.tab()
      expect(target()).toHaveFocus()
    }

    await screen.findByRole('list', { name: 'Days' })
    await tabTo(() => screen.getByRole('button', { name: /^Friday 9 October 2026/ }))
    await user.keyboard('{Enter}')
    await tabTo(() => screen.getByRole('button', { name: /^10:15 to 10:30/ }))
    await user.keyboard('{Enter}')
    await tabTo(() => screen.getByRole('link', { name: 'Continue to booking' }))
    await user.keyboard('{Enter}')

    expect(await title('Booking')).toHaveFocus()
    expect(router.state.location.pathname).toBe(BOOK_PATH)

    // From the heading, the reason is the first stop; Enter in it is a new line, not a booking.
    await user.tab()
    expect(reasonField()).toHaveFocus()
    await user.keyboard('Short of breath{Enter}on the stairs')
    expect(booking.requests).toHaveLength(0)
    await user.tab()
    expect(await confirmButton()).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'Choose another time' })).toHaveFocus()
    await user.tab({ shift: true })
    await user.keyboard('{Enter}')

    expect(await title('Appointment booked')).toHaveFocus()
    expect(booking.requests).toHaveLength(1)
    expect(booking.requests[0].body).toEqual({ start: START, end: END, type: 'new', reason: 'Short of breath\non the stairs' })

    // The reference leads to the appointment's own page; then the ways on, the appointments first.
    await user.tab()
    expect(screen.getByRole('link', { name: appointmentRef(1) })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'View my appointments' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'Back to home' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'Back to the doctor' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(await title('Asha Rao')).toHaveFocus()
  })

  it('in every state: one h1, every control named and 44 px, icons hidden, nothing jumps the tab order, one live region', async () => {
    const { booking } = setup()
    booking.next(unreachable, fail(503, 'SERVICE_UNAVAILABLE'))
    const { user } = open()
    const region = () => live()
    const sound = () => {
      const touchSized = /(^|\s)(h-11|min-h-11|min-h-24|size-11)(\s|$)/
      for (const control of main().querySelectorAll('a, button, textarea')) expect(control.className).toMatch(touchSized)
      for (const control of main().querySelectorAll<HTMLElement>('a, button, textarea')) expect(control).toHaveAccessibleName()
      for (const element of document.querySelectorAll('[tabindex]')) expect(element).toHaveAttribute('tabindex', '-1')
      for (const icon of document.querySelectorAll('svg')) expect(icon).toHaveAttribute('aria-hidden', 'true')
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
      expect(screen.getAllByRole('status')).toHaveLength(1)
      expect(main().querySelector('table, img, input, select')).toBeNull()
    }

    await confirmButton()
    const first = region()
    sound()
    // The emergency notice is a note, read in its place: it does not interrupt as an alert would.
    expect(screen.getByRole('note')).toHaveTextContent(/^This app is not for emergencies\./)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await user.click(await confirmButton())
    await screen.findByText('No connection')
    sound()
    await user.click(await retryButton())
    await screen.findByText('We could not book the appointment')
    sound()
    await user.click(await retryButton())
    await title('Appointment booked')
    sound()

    // The same node throughout, so each change is announced; a replaced node would not be.
    expect(region()).toBe(first)
    expect(first).toHaveTextContent(/^Appointment booked$/)
  })
})

describe('booking — telling the failures apart', () => {
  const answered = (status: number, code: string, message = 'server wording') => new ApiError(message, code, status)

  it.each([
    ['409, the slot taken', answered(409, 'RESOURCE_CONFLICT', 'This time is no longer available.'), 'slot_taken'],
    ['409, the key reused', answered(409, 'RESOURCE_CONFLICT', 'This request was already used for another booking.'), 'slot_taken'],
    ['409, anything else', answered(409, 'SOMETHING'), 'slot_taken'],
    ['400, not a bookable slot', answered(400, 'BUSINESS_RULE_VIOLATION', 'This time cannot be booked.'), 'not_bookable'],
    ['400, anything else', answered(400, 'BUSINESS_RULE_VIOLATION'), 'not_bookable'],
    ['400, the overlap message under another code', answered(400, 'SOMETHING', 'You already have an appointment at this time.'), 'not_bookable'],
    ['400, the patient’s own overlap', answered(400, 'BUSINESS_RULE_VIOLATION', 'You already have an appointment at this time.'), 'own_overlap'],
    ['400, the limit', answered(400, 'BUSINESS_RULE_VIOLATION', 'You have reached the limit of upcoming appointments at this hospital.'), 'limit_reached'],
    ['400, the limit worded differently', answered(400, 'BUSINESS_RULE_VIOLATION', 'you have reached the limit'), 'not_bookable'],
    ['403, no record link', answered(403, 'RECORD_LINK_REQUIRED'), 'link_required'],
    ['403, a pending policy', answered(403, 'CONSENT_REQUIRED'), 'policies_pending'],
    ['403, anything else', answered(403, 'FORBIDDEN'), 'server'],
    ['401, with no refresh to be had', answered(401, 'AUTHENTICATION_REQUIRED'), 'server'],
    ['404', answered(404, 'RESOURCE_NOT_FOUND', 'Not Found'), 'not_found'],
    ['422', answered(422, 'VALIDATION_ERROR'), 'invalid'],
    ['429', answered(429, 'RATE_LIMITED'), 'server'],
    ['500', answered(500, 'INTERNAL_ERROR'), 'server'],
    ['503 with no envelope', answered(503, 'network_error'), 'server'],
    ['no answer at all', new ApiError('Network Error', 'network_error'), 'offline'],
    ['a 2xx that is not an appointment', new ApiError('Response is not an appointment', 'bad_response'), 'unconfirmed'],
    ['an error of no known kind', new ApiError('?'), 'server'],
    ['something that is not an error of the API', new TypeError('boom'), 'server'],
    ['nothing', undefined, 'server'],
  ])('%s', (_case, error, kind) => {
    expect(classifyBookingError(error)).toBe(kind)
  })

  it('only a lost or unreadable answer is asked for again; a refusal never is', () => {
    const retriable = ['offline', 'server', 'unconfirmed', 'policies_pending'] as const
    const final = ['slot_taken', 'not_bookable', 'own_overlap', 'limit_reached', 'link_required', 'not_found', 'invalid'] as const
    for (const kind of retriable) expect(isRetriable(kind)).toBe(true)
    for (const kind of final) expect(isRetriable(kind)).toBe(false)
  })
})
