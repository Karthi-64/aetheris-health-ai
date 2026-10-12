import { act, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { deferred, fail, headerOf, ok, okPage, serve, unreachable, type Handler } from '@/test/fakeApi'
import { doctorRef } from '@/test/fixtures'
import {
  anAppointment,
  manyAppointments,
  MY_APPOINTMENTS as LIST,
  myAppointmentRef,
  myAppointmentsEndpoints,
  type FakeMyAppointmentsOptions,
  type WireAppointment,
} from '@/test/myAppointments'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

/**
 * The patient's own appointments, as a list. The tests hold the page to what
 * it asks the server for (a scope and a page, nothing else), to showing every
 * time on the hospital's clock, to offering "Cancel" only on the server's
 * word, and to putting on screen nothing a response carries beyond what the
 * contract names.
 */

const REFRESH = 'POST /auth/refresh'

const upcoming = anAppointment()
const completed = anAppointment({
  ref: myAppointmentRef(2),
  status: 'completed',
  start: '2026-09-21T16:00:00+05:30',
  end: '2026-09-21T16:15:00+05:30',
  doctor: { ref: doctorRef(2), name: 'Vikram Shah', specialization: 'General Medicine' },
})
const cancelled = anAppointment({
  ref: myAppointmentRef(3),
  status: 'cancelled',
  start: '2026-10-20T11:00:00+05:30',
  end: '2026-10-20T11:30:00+05:30',
  hospital: { ref: 'lakeside-clinic', name: 'Lakeside Clinic' },
  doctor: { ref: doctorRef(3), name: 'Meera Iyer', specialization: 'Joint Replacement' },
})

function setup(appointments: WireAppointment[] = [upcoming, completed, cancelled], options: FakeMyAppointmentsOptions = {}) {
  // Copies, so one test's cancellation is not another test's starting point.
  const mine = myAppointmentsEndpoints(appointments.map((each) => ({ ...each })), options)
  const api = serve(mine.routes)
  return { api, mine }
}

function open(path = '/appointments') {
  signIn('access-1')
  return renderApp(path)
}

const main = () => screen.getByRole('main')
const title = () => screen.findByRole('heading', { level: 1, name: 'My appointments' })
const tab = (name: 'Upcoming' | 'Past') => screen.getByRole('tab', { name })
const panel = () => screen.getByRole('tabpanel')
const position = () => screen.getByRole('status')
const cards = () => within(within(panel()).getByRole('list')).getAllByRole('listitem')
const card = (doctor: string) => screen.findByRole('heading', { level: 3, name: doctor }).then((name) => name.closest('li')!)
const paramsOf = (request: { params?: unknown }) => request.params

describe('my appointments — the list', () => {
  it('shows skeletons while the first page loads, then the appointments', async () => {
    const answer = deferred()
    const { mine } = setup()
    serve({ ...mine.routes, [LIST]: answer.handler })
    open()

    expect(await title()).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('My appointments · Atheris Health'))
    expect(position()).toHaveTextContent('Loading appointments…')
    expect(panel().querySelectorAll('[data-slot="skeleton"]')).toHaveLength(3)
    expect(within(panel()).queryByRole('list')).not.toBeInTheDocument()

    await act(async () => answer.answer(okPage([mine.wireOf(upcoming.ref as string)])))

    expect(await card('Asha Menon')).toBeInTheDocument()
    expect(panel().querySelectorAll('[data-slot="skeleton"]')).toHaveLength(0)
    expect(position()).toHaveTextContent(/^1 appointment$/)
  })

  it('asks for the UPCOMING scope first: a scope, a page and a page size, and nothing else', async () => {
    const { api } = setup()
    open()

    await card('Asha Menon')
    expect(api.sent.map((request) => `${request.method?.toUpperCase()} ${request.url}`)).toEqual([LIST])
    expect(paramsOf(api.sent[0])).toEqual({ scope: 'upcoming', page: 1, page_size: 20 })
    expect(api.sent[0].data).toBeUndefined()

    expect(tab('Upcoming')).toHaveAttribute('aria-selected', 'true')
    expect(tab('Past')).toHaveAttribute('aria-selected', 'false')
    // Only what the server calls upcoming is here.
    expect(cards()).toHaveLength(1)
    expect(main()).not.toHaveTextContent(/Vikram Shah|Meera Iyer/)
  })

  it('a card says who with, where, when and in what state, and leads to the appointment', async () => {
    setup()
    open()

    const item = await card('Asha Menon')
    expect(within(item).getByText('Cardiology')).toBeInTheDocument()
    expect(within(item).getByText('City Care')).toBeInTheDocument()
    expect(within(item).getByText('Monday 12 October 2026')).toBeInTheDocument()
    expect(within(item).getByText('10:00 – 10:15 (Asia/Kolkata)')).toBeInTheDocument()
    expect(within(item).getByText('Booked')).toBeInTheDocument()
    // Named with the doctor and the day, so two cards do not have two identical links.
    expect(within(item).getByRole('link', { name: 'View appointment Asha Menon Monday 12 October 2026' })).toHaveAttribute(
      'href',
      `/appointments/${upcoming.ref}`,
    )
  })

  it('switching to PAST asks for that scope, puts it in the URL, and Back returns to upcoming', async () => {
    const { api } = setup()
    const { user, router } = open()
    await card('Asha Menon')

    await user.click(tab('Past'))

    expect(await card('Meera Iyer')).toBeInTheDocument()
    expect(router.state.location.search).toBe('?view=past')
    expect(api.calls(LIST).map(paramsOf)).toEqual([
      { scope: 'upcoming', page: 1, page_size: 20 },
      { scope: 'past', page: 1, page_size: 20 },
    ])
    expect(tab('Past')).toHaveAttribute('aria-selected', 'true')
    expect(panel()).toHaveAccessibleName('Past')
    // Latest first, as the server orders the past.
    expect(cards().map((each) => within(each).getByRole('heading').textContent)).toEqual(['Meera Iyer', 'Vikram Shah'])
    expect(main()).not.toHaveTextContent('Asha Menon')

    await act(() => router.navigate(-1))
    expect(await card('Asha Menon')).toBeInTheDocument()
    expect(router.state.location.search).toBe('')
    expect(tab('Upcoming')).toHaveAttribute('aria-selected', 'true')
  })

  it('RELOAD — the view and the page come back from the URL', async () => {
    const { api } = setup([...manyAppointments(45, { status: 'completed' })])
    open('/appointments?view=past&page=2')

    expect(await card('Doctor 25')).toBeInTheDocument()
    expect(api.calls(LIST).map(paramsOf)).toEqual([{ scope: 'past', page: 2, page_size: 20 }])
    expect(tab('Past')).toHaveAttribute('aria-selected', 'true')
    expect(position()).toHaveTextContent('Showing 21–40 of 45 appointments')
  })

  it.each([
    ['?view=all', { scope: 'upcoming', page: 1, page_size: 20 }],
    ['?view=PAST', { scope: 'upcoming', page: 1, page_size: 20 }],
    ['?view=past&page=0', { scope: 'past', page: 1, page_size: 20 }],
    ['?page=-3', { scope: 'upcoming', page: 1, page_size: 20 }],
    ['?page=1001', { scope: 'upcoming', page: 1, page_size: 20 }],
    ['?page=2abc&scope=past&page_size=500&patient_id=7', { scope: 'upcoming', page: 1, page_size: 20 }],
  ])('ATTACK — a hand-edited address %s asks only for what the server offers', async (query, params) => {
    const { api } = setup()
    open(`/appointments${query}`)

    await title()
    await waitFor(() => expect(api.calls(LIST)).toHaveLength(1))
    expect(paramsOf(api.calls(LIST)[0])).toEqual(params)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('my appointments — the hospital’s clock', () => {
  it('shows the day and the time in the HOSPITAL’s zone, and names it: Los Angeles, twelve and a half hours behind the test', async () => {
    // 02:30Z on the 13th is 19:30 on Monday the 12th in Los Angeles — and 08:00 on Tuesday the 13th here.
    const losAngeles = anAppointment({
      start: '2026-10-13T02:30:00Z',
      end: '2026-10-13T02:45:00Z',
      timezone: 'America/Los_Angeles',
      hospital: { ref: 'bay-clinic', name: 'Bay Clinic' },
    })
    setup([losAngeles])
    open()

    const item = await card('Asha Menon')
    expect(within(item).getByText('Monday 12 October 2026')).toBeInTheDocument()
    expect(within(item).getByText('19:30 – 19:45 (America/Los_Angeles)')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Tuesday|13 October|08:00|08:15|02:30|02:45/)
  })

  it('a zone fourteen hours ahead of UTC lands on the next day, as it does at the hospital', async () => {
    setup([anAppointment({ start: '2026-10-12T22:00:00Z', end: '2026-10-12T22:15:00Z', timezone: 'Pacific/Kiritimati' })])
    open()

    const item = await card('Asha Menon')
    expect(within(item).getByText('Tuesday 13 October 2026')).toBeInTheDocument()
    expect(within(item).getByText('12:00 – 12:15 (Pacific/Kiritimati)')).toBeInTheDocument()
  })

  it('a zone the browser does not know: the server’s own offset is the hospital’s clock, never the browser’s', async () => {
    setup([anAppointment({ start: '2026-10-12T23:30:00-08:00', end: '2026-10-12T23:45:00-08:00', timezone: 'Mars/Olympus' })])
    open()

    const item = await card('Asha Menon')
    expect(within(item).getByText('Monday 12 October 2026')).toBeInTheDocument()
    expect(within(item).getByText('23:30 – 23:45 (Mars/Olympus)')).toBeInTheDocument()
  })
})

describe('my appointments — the status is the server’s, in the app’s word for it', () => {
  it.each([
    ['booked', 'Booked'],
    ['checked_in', 'Checked in'],
    ['in_progress', 'In progress'],
    ['completed', 'Completed'],
    ['cancelled', 'Cancelled'],
    ['no_show', 'Missed'],
  ])('%s is shown as "%s"', async (status, label) => {
    const { mine } = setup([anAppointment({ status })])
    // Answered whatever the view, so each state can be seen on a card.
    serve({ ...mine.routes, [LIST]: okPage([mine.wireOf(upcoming.ref as string)]) })
    open()

    const item = await card('Asha Menon')
    const labels = ['Booked', 'Checked in', 'In progress', 'Completed', 'Cancelled', 'Missed']
    expect(within(item).getByText(label)).toBeInTheDocument()
    for (const other of labels.filter((each) => each !== label)) expect(within(item).queryByText(other)).not.toBeInTheDocument()
    // The server's own word is never shown.
    expect(main()).not.toHaveTextContent(/checked_in|in_progress|no_show/)
  })

  it.each([
    ['a status the contract does not have', { status: 'pending' }],
    ['a status in other capitals', { status: 'Booked' }],
    ['no status', { status: undefined }],
    ['no reference', { ref: undefined }],
    ['a reference that is not one', { ref: '17' }],
    ['a start without an offset', { start: '2026-10-12T10:00:00' }],
    ['an end that is not after the start', { end: '2026-10-12T10:00:00+05:30' }],
    ['no time zone', { timezone: '  ' }],
    ['a hospital without a name', { hospital: { ref: 'city-care' } }],
    ['a doctor without a name', { doctor: { ref: doctorRef(1), name: '', specialization: 'Cardiology' } }],
    ['an entry that is not an object', null],
  ])('MALFORMED — one entry with %s fails the read: no card is shown, and none is silently left out', async (_case, broken) => {
    const { mine } = setup()
    const good = mine.wireOf(upcoming.ref as string)
    serve({ ...mine.routes, [LIST]: okPage([good, broken === null ? 'nonsense' : { ...good, ref: myAppointmentRef(9), ...broken }]) })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load your appointments. Please try again.')
    expect(within(panel()).queryByRole('list')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent('Asha Menon')
    expect(position()).toBeEmptyDOMElement()
  })

  it.each([
    ['an object where the list should be', ok({ items: [] })],
    ['a list without its pagination', ok([])],
    ['text where the list should be', { status: 200, data: { success: true, message: 'ok', data: 'none', metadata: { pagination: { page: 1, page_size: 20, total_records: 0, total_pages: 0 } } } }],
    ['pagination that is not numbers', { status: 200, data: { success: true, message: 'ok', data: [], metadata: { pagination: { page: '1', page_size: 20, total_records: 'many', total_pages: 1 } } } }],
    ['no body at all', { status: 200, data: '' }],
  ])('MALFORMED — %s is a failed read, never an empty list', async (_case, outcome) => {
    serve({ [LIST]: outcome })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load your appointments. Please try again.')
    expect(main()).not.toHaveTextContent(/No upcoming appointments/)
    expect(screen.queryByRole('link', { name: 'Find a hospital' })).not.toBeInTheDocument()
  })
})

describe('my appointments — "Cancel" is offered on the server’s word only', () => {
  it('an upcoming appointment the server says can be cancelled has a "Cancel" link to its own page — which asks first', async () => {
    const { api, mine } = setup()
    open()

    const item = await card('Asha Menon')
    expect(mine.wireOf(upcoming.ref as string).can_cancel).toBe(true)
    expect(within(item).getByRole('link', { name: 'Cancel Asha Menon Monday 12 October 2026' })).toHaveAttribute(
      'href',
      `/appointments/${upcoming.ref}?cancel=1`,
    )
    // It is a link, not a button: nothing on the list cancels anything.
    expect(main().querySelector('form')).toBeNull()
    expect(within(panel()).queryByRole('button', { name: /cancel/i })).not.toBeInTheDocument()
    expect(api.sent.every((request) => request.method?.toLowerCase() === 'get')).toBe(true)
    expect(mine.cancelRequests).toHaveLength(0)
  })

  it.each<[string, (appointment: WireAppointment) => WireAppointment]>([
    ['the text "true"', (appointment) => ({ ...appointment, can_cancel: 'true' })],
    ['the number 1', (appointment) => ({ ...appointment, can_cancel: 1 })],
    ['an object', (appointment) => ({ ...appointment, can_cancel: { value: true } })],
    ['null', (appointment) => ({ ...appointment, can_cancel: null })],
    ['false', (appointment) => ({ ...appointment, can_cancel: false })],
    [
      'nothing at all',
      (appointment) => {
        const { can_cancel: _dropped, ...rest } = appointment
        return rest
      },
    ],
  ])('ATTACK — can_cancel that is %s offers no cancellation, though the appointment is booked and days away', async (_case, shape) => {
    setup([upcoming], { shape })
    open()

    const item = await card('Asha Menon')
    expect(within(item).getByText('Booked')).toBeInTheDocument()
    expect(within(item).getByRole('link', { name: /^View appointment/ })).toBeInTheDocument()
    expect(within(item).queryByRole('link', { name: /cancel/i })).not.toBeInTheDocument()
    expect(item.innerHTML).not.toContain('cancel=1')
  })

  it('a booked appointment past the hospital’s cut-off has no "Cancel" link', async () => {
    // Ninety minutes away: inside the two hours the hospital asks for.
    const { mine } = setup([anAppointment({ start: '2026-10-07T10:30:00+05:30', end: '2026-10-07T10:45:00+05:30' })])
    open()

    const item = await card('Asha Menon')
    expect(mine.wireOf(upcoming.ref as string).can_cancel).toBe(false)
    expect(within(item).queryByRole('link', { name: /cancel/i })).not.toBeInTheDocument()
  })

  it('nothing in the PAST view has a "Cancel" link, whatever the response says', async () => {
    setup([completed, cancelled], { shape: (appointment) => ({ ...appointment, can_cancel: true }) })
    open('/appointments?view=past')

    await card('Meera Iyer')
    expect(cards()).toHaveLength(2)
    expect(within(panel()).queryByRole('link', { name: /cancel/i })).not.toBeInTheDocument()
    expect(panel().innerHTML).not.toContain('cancel=1')
    expect(within(panel()).queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('my appointments — empty states', () => {
  it('NO UPCOMING — says so, and offers the way to finding a hospital', async () => {
    setup([completed])
    open()

    expect(await screen.findByText('When you book an appointment, it will be listed here.')).toBeInTheDocument()
    expect(within(panel()).getAllByText('No upcoming appointments')).toHaveLength(2)
    expect(position()).toHaveTextContent(/^No upcoming appointments$/)
    expect(within(panel()).queryByRole('list')).not.toBeInTheDocument()

    expect(screen.getByRole('link', { name: 'Find a hospital' })).toHaveAttribute('href', '/hospitals')
  })

  it('NO PAST — says so, differently, with nothing to press', async () => {
    setup([upcoming])
    open('/appointments?view=past')

    expect(await screen.findByText('Appointments that are over, cancelled or missed will be listed here.')).toBeInTheDocument()
    expect(position()).toHaveTextContent(/^No past appointments$/)
    expect(within(panel()).queryByRole('link')).not.toBeInTheDocument()
    expect(within(panel()).queryByRole('button')).not.toBeInTheDocument()
  })
})

describe('my appointments — pagination', () => {
  it('pages through the results in the server’s numbers, keeps the view, and moves focus to the top of them', async () => {
    const { api } = setup(manyAppointments(45))
    const { user, router } = open()

    expect(await card('Doctor 01')).toBeInTheDocument()
    expect(cards()).toHaveLength(20)
    expect(position()).toHaveTextContent('Showing 1–20 of 45 appointments')
    const pages = screen.getByRole('navigation', { name: 'Pages of appointments' })
    expect(within(pages).getByText('Page 1 of 3')).toBeInTheDocument()
    expect(within(pages).getByRole('button', { name: 'Previous' })).toBeDisabled()

    await user.click(within(pages).getByRole('button', { name: 'Next' }))

    expect(await card('Doctor 21')).toBeInTheDocument()
    expect(router.state.location.search).toBe('?page=2')
    expect(paramsOf(api.calls(LIST)[1])).toEqual({ scope: 'upcoming', page: 2, page_size: 20 })
    expect(position()).toHaveTextContent('Showing 21–40 of 45 appointments')
    expect(screen.getByRole('heading', { level: 2, name: 'Upcoming appointments' })).toHaveFocus()

    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await card('Doctor 41')).toBeInTheDocument()
    expect(cards()).toHaveLength(5)
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()

    // Another view starts from its own first page.
    await user.click(tab('Past'))
    await waitFor(() => expect(router.state.location.search).toBe('?view=past'))
    expect(paramsOf(api.calls(LIST).at(-1)!)).toEqual({ scope: 'past', page: 1, page_size: 20 })
  })

  it('one page of results has no pagination at all', async () => {
    setup()
    open()

    await card('Asha Menon')
    expect(screen.queryByRole('navigation', { name: 'Pages of appointments' })).not.toBeInTheDocument()
  })

  it('BEYOND THE END — says the page is empty, how many there are, and leads back to the first', async () => {
    setup(manyAppointments(3))
    const { user, router } = open('/appointments?page=9')

    expect(await screen.findByText('There is nothing on this page')).toBeInTheDocument()
    expect(screen.getByText('There are 3 appointments in all, on earlier pages.')).toBeInTheDocument()
    expect(position()).toHaveTextContent(/^Nothing on this page$/)
    expect(screen.queryByText('No upcoming appointments')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Go to the first page' }))
    expect(await card('Doctor 01')).toBeInTheDocument()
    expect(router.state.location.search).toBe('')
  })
})

describe('my appointments — when it cannot load', () => {
  afterEach(() => onlineManager.setOnline(true))

  it('ERROR — the app’s own message, never the server’s, and a retry that works', async () => {
    const { api, mine } = setup()
    api.on({ [LIST]: fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)') })
    const { user } = open()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('We could not load your appointments. Please try again.')
    expect(document.body).not.toHaveTextContent('Traceback')
    // A failure is not dressed up as an empty list.
    expect(main()).not.toHaveTextContent(/No upcoming appointments/)
    expect(position()).toBeEmptyDOMElement()

    api.on(mine.routes)
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await card('Asha Menon')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(api.calls(LIST)).toHaveLength(2)
  })

  it('SERVER DOWN (503) is a failure on the server’s side, not a connection problem', async () => {
    serve({ [LIST]: fail(503, 'SERVICE_UNAVAILABLE', 'upstream connect error') })
    open()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/^We could not load your appointments\. Please try again\.$/)
    expect(document.body).not.toHaveTextContent(/No connection|upstream/)
  })

  it('OFFLINE — a request that gets no answer is called a connection problem, and can be retried', async () => {
    const { api, mine } = setup()
    api.on({ [LIST]: unreachable })
    const { user } = open()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('No connection')
    expect(alert).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
    expect(screen.queryByText(/We could not load your appointments/)).not.toBeInTheDocument()

    api.on(mine.routes)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(await card('Asha Menon')).toBeInTheDocument()
  })

  it('OFFLINE — when the browser says it has no network, that is what the patient is told', async () => {
    vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false)
    serve({
      [LIST]: () => {
        throw new Error('Failed to fetch')
      },
    })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
    expect(document.body).not.toHaveTextContent('Failed to fetch')
  })

  it('OFFLINE — a request held back for lack of a network is not shown as loading for ever, and resumes by itself', async () => {
    onlineManager.setOnline(false)
    const { api } = setup()
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
    expect(screen.queryByText('Loading appointments…')).not.toBeInTheDocument()
    expect(api.sent).toHaveLength(0)

    act(() => onlineManager.setOnline(true))
    expect(await card('Asha Menon')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it.each([
    ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot show this in the app right now\. Please try again later\.$/],
    ['the rate limit (429)', fail(429, 'RATE_LIMITED', 'server wording'), /^Too many requests\. Please wait a moment and try again\.$/],
    ['a refused parameter (422)', fail(422, 'VALIDATION_ERROR', 'server wording'), /^We could not load your appointments\. Please try again\.$/],
  ])('shows a safe message for %s', async (_case, outcome, message) => {
    serve({ [LIST]: outcome })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(document.body).not.toHaveTextContent('server wording')
  })
})

describe('my appointments — the session', () => {
  it('an expired token is refreshed once and the same list is asked for again', async () => {
    const { api, mine } = setup()
    // The token each attempt carried, as it left: the retry reuses the request it was refused with.
    const tokens: (string | undefined)[] = []
    api.on({
      [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }),
      [LIST]: (config) => {
        tokens.push(headerOf(config, 'Authorization'))
        return tokens.length === 1 ? fail(401, 'AUTHENTICATION_REQUIRED') : (mine.routes[LIST] as Handler)(config)
      },
    })
    open()

    expect(await card('Asha Menon')).toBeInTheDocument()
    expect(api.calls(REFRESH)).toHaveLength(1)
    expect(api.calls(LIST).map(paramsOf)).toEqual([
      { scope: 'upcoming', page: 1, page_size: 20 },
      { scope: 'upcoming', page: 1, page_size: 20 },
    ])
    expect(tokens).toEqual(['Bearer access-1', 'Bearer access-new'])
  })

  it('a session that cannot be refreshed ends at the sign-in page, with no appointment left on screen', async () => {
    serve({ [REFRESH]: fail(401, 'UNAUTHORIZED'), [LIST]: fail(401, 'AUTHENTICATION_REQUIRED') })
    const { router } = open()

    expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(isSignedIn()).toBe(false)
  })
})

describe('my appointments — what a response may put on the screen', () => {
  const SMUGGLED = {
    id: 'INTERNAL-ID-SECRET',
    patient_id: 'PATIENT-ID-SECRET',
    hospital_id: 'HOSPITAL-ID-SECRET',
    notes: 'NOTES-SECRET',
    cancelled_reason: 'CANCELLED-REASON-SECRET',
    cancelled_by: 'CANCELLED-BY-SECRET',
    created_by: 'CREATED-BY-SECRET',
    idempotency_key: 'IDEMPOTENCY-KEY-SECRET',
    audit: { actor: 'AUDIT-SECRET' },
    type: 'TYPE-SECRET',
  }

  it('ATTACK — fields the contract does not name never reach a card: not the text, not an attribute, not a link', async () => {
    setup([upcoming, completed, cancelled], {
      shape: (appointment) => ({
        ...appointment,
        ...SMUGGLED,
        hospital: { ...(appointment.hospital as object), hospital_id: 'NESTED-HOSPITAL-SECRET', phone: 'PHONE-SECRET' },
        doctor: { ...(appointment.doctor as object), user_id: 'USER-ID-SECRET', fee: 'FEE-SECRET' },
      }),
    })
    const { user } = open()

    await card('Asha Menon')
    expect(document.documentElement.outerHTML).not.toMatch(/SECRET/)
    await user.click(tab('Past'))
    await card('Meera Iyer')
    expect(document.documentElement.outerHTML).not.toMatch(/SECRET/)
  })

  it('the patient’s own reason for the visit is not put on a card either', async () => {
    setup([anAppointment({ reason: 'Chest pain since Tuesday' })])
    open()

    await card('Asha Menon')
    expect(document.documentElement.outerHTML).not.toContain('Chest pain')
  })

  it('references are in the links and nowhere else: no UUID is shown, kept in an attribute, or read out', async () => {
    setup()
    const { user } = open()
    const withoutLinks = () => main().innerHTML.replace(/ href="[^"]*"/g, '')
    const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

    await card('Asha Menon')
    expect(main()).not.toHaveTextContent(UUID)
    expect(withoutLinks()).not.toMatch(UUID)
    // The appointment's own reference is in its two links; the doctor's and the hospital's are in none.
    const hrefs = [...main().querySelectorAll('a')].map((link) => link.getAttribute('href'))
    expect(hrefs).toEqual([`/appointments/${upcoming.ref}`, `/appointments/${upcoming.ref}?cancel=1`])
    expect(main().innerHTML).not.toContain(doctorRef(1))

    await user.click(tab('Past'))
    await card('Meera Iyer')
    expect(main()).not.toHaveTextContent(UUID)
    expect(withoutLinks()).not.toMatch(UUID)
  })

  it('ATTACK — markup in a name, a specialisation, a hospital or a time zone is text: shown as written, never run', async () => {
    const hostile = anAppointment({
      timezone: '<svg onload=window.pwned=1>',
      start: '2026-10-12T10:00:00+05:30',
      hospital: { ref: 'city-care', name: '<img src=x onerror=window.pwned=1>' },
      doctor: { ref: doctorRef(1), name: '<script>window.pwned=1</script>', specialization: '<b onmouseover=window.pwned=1>Cardiology</b>' },
    })
    setup([hostile])
    open()

    const item = await card('<script>window.pwned=1</script>')
    expect(within(item).getByText('<img src=x onerror=window.pwned=1>')).toBeInTheDocument()
    expect(within(item).getByText('<b onmouseover=window.pwned=1>Cardiology</b>')).toBeInTheDocument()
    expect(within(item).getByText('10:00 – 10:15 (<svg onload=window.pwned=1>)')).toBeInTheDocument()
    expect(main().querySelector('img, script, b, [onerror], [onload], [onmouseover]')).toBeNull()
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined()
  })
})

describe('my appointments — keyboard and assistive technology', () => {
  it('the tabs are one stop: arrows, Home and End move between them, and the one in focus is the one shown', async () => {
    const { api } = setup()
    const { user, router } = open()
    await card('Asha Menon')

    // One stop for Tab: the tab that is not selected is skipped.
    expect(tab('Upcoming')).not.toHaveAttribute('tabindex')
    expect(tab('Past')).toHaveAttribute('tabindex', '-1')
    expect(screen.getByRole('tablist')).toHaveAccessibleName('Appointments to show')
    expect(tab('Upcoming')).toHaveAttribute('aria-controls', panel().id)

    tab('Upcoming').focus()
    await user.keyboard('{ArrowRight}')
    expect(tab('Past')).toHaveFocus()
    expect(tab('Past')).toHaveAttribute('aria-selected', 'true')
    expect(await card('Meera Iyer')).toBeInTheDocument()
    expect(router.state.location.search).toBe('?view=past')
    expect(tab('Past')).not.toHaveAttribute('tabindex')
    expect(tab('Upcoming')).toHaveAttribute('tabindex', '-1')

    await user.keyboard('{ArrowRight}')
    expect(tab('Upcoming')).toHaveFocus()
    expect(await card('Asha Menon')).toBeInTheDocument()
    await user.keyboard('{End}')
    expect(tab('Past')).toHaveFocus()
    await user.keyboard('{Home}')
    expect(tab('Upcoming')).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(tab('Past')).toHaveFocus()
    await waitFor(() => expect(router.state.location.search).toBe('?view=past'))

    // From the tabs, Tab goes on into the appointments, and Enter opens one.
    await card('Meera Iyer')
    await user.tab()
    expect(screen.getByRole('link', { name: /^View appointment Meera Iyer/ })).toHaveFocus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(router.state.location.pathname).toBe(`/appointments/${cancelled.ref}`))
    // Each view was read once: going back and forth between them asked for nothing more.
    expect(api.calls(LIST)).toHaveLength(2)
  })

  it('one h1, one live region that stays and changes its text, every control named and 44 px, icons hidden', async () => {
    setup(manyAppointments(45))
    const { user } = open()
    const sound = () => {
      const touchSized = /(^|\s)(h-11|min-h-11|size-11)(\s|$)/
      const controls = [...main().querySelectorAll<HTMLElement>('a, button')]
      for (const control of controls) expect(control.className).toMatch(touchSized)
      for (const control of controls) expect(control).toHaveAccessibleName()
      for (const icon of document.querySelectorAll('svg')) expect(icon).toHaveAttribute('aria-hidden', 'true')
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
      expect(screen.getAllByRole('status')).toHaveLength(1)
      expect(main().querySelector('table, img, time, form, input, select, textarea')).toBeNull()
      // Nothing jumps the tab order: the only tabindex is -1.
      for (const element of document.querySelectorAll('[tabindex]')) expect(element).toHaveAttribute('tabindex', '-1')
    }

    await title()
    const region = position()
    await card('Doctor 01')
    sound()
    expect(within(main()).getAllByRole('link', { name: /^View appointment / })).toHaveLength(20)
    expect(new Set(within(main()).getAllByRole('link').map((link) => link.getAttribute('aria-labelledby'))).size).toBe(40)

    await user.click(tab('Past'))
    await screen.findByText('No past appointments', { selector: 'p.font-display' })
    sound()
    expect(position()).toBe(region)

    const navigation = within(screen.getByRole('navigation', { name: 'Main' })).getAllByRole('link')
    expect(navigation.map((link) => link.textContent)).toEqual(['Home', 'Hospitals', 'Appointments', 'Link hospital'])
    for (const link of navigation) expect(link).toHaveClass('min-h-14')
    expect(screen.getByRole('link', { name: 'Appointments' })).toHaveAttribute('aria-current', 'page')
  })
})
