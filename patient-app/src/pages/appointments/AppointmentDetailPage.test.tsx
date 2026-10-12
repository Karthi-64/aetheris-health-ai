import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { AxiosError } from 'axios'
import { afterEach, describe, expect, it } from 'vitest'
import { ApiError } from '@atheris/api-core'
import { classifyCancelError, isCancelRetriable, toMyAppointment } from '@/api/myAppointments'
import { LOST, type ScriptedAnswer } from '@/test/booking'
import { doctorDirectory } from '@/test/doctorDirectory'
import { deferred, fail, noContent, ok, serve, unreachable, type Routes } from '@/test/fakeApi'
import { ashaRao, cityCareHospital, doctorRef, me } from '@/test/fixtures'
import {
  anAppointment,
  cancelRefusals,
  cancelRoute,
  MY_APPOINTMENTS as LIST,
  myAppointmentRef,
  myAppointmentRoute,
  myAppointmentsEndpoints,
  type FakeMyAppointmentsOptions,
  type WireAppointment,
} from '@/test/myAppointments'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

/**
 * One appointment, and cancelling it. The tests hold the page to four things:
 * the cancel action exists only on the server's word (`can_cancel === true`);
 * one confirmation sends exactly one request, with a reason and nothing else;
 * "cancelled" is said only after the server has said it, and only from what
 * the server answered; and every refusal ends with the appointment as the
 * server now describes it.
 */

const REF = myAppointmentRef(1)
const DETAIL = myAppointmentRoute(REF)
const CANCEL = cancelRoute(REF)
const PATH = `/appointments/${REF}`
const REFRESH = 'POST /auth/refresh'

function setup(appointments: WireAppointment[] = [anAppointment()], options: FakeMyAppointmentsOptions = {}, more: Routes = {}) {
  // Copies, so one test's cancellation is not another test's starting point.
  const mine = myAppointmentsEndpoints(appointments.map((each) => ({ ...each })), options)
  const api = serve({ ...mine.routes, ...more })
  return { api, mine }
}

function open(path = PATH) {
  signIn('access-1')
  return renderApp(path)
}

const main = () => screen.getByRole('main')
const title = (name = 'Appointment') => screen.findByRole('heading', { level: 1, name })
const details = () => screen.findByRole('region', { name: 'Appointment details' })
const valueOf = (region: HTMLElement, label: string) => within(region).getByText(label).nextElementSibling as HTMLElement
const live = () => screen.getByRole('status')
const routeOf = (request: { method?: string; url?: string }) => `${request.method?.toUpperCase()} ${request.url}`

const cancelButton = async () => within(await screen.findByRole('main')).findByRole('button', { name: 'Cancel appointment' })
const noCancelButton = () => expect(within(main()).queryByRole('button', { name: /cancel/i })).not.toBeInTheDocument()
const dialog = () => screen.findByRole('dialog', { name: 'Cancel this appointment?' })
const noDialog = () => expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
const inDialog = () => within(screen.getByRole('dialog'))
const reason = (name: string) => inDialog().getByRole('radio', { name })
const detailsField = () => inDialog().getByRole('textbox', { name: 'More details (optional)' })
const keepButton = () => inDialog().getByRole('button', { name: 'Keep appointment' })
const submitButton = () => inDialog().getByRole('button', { name: /^(Cancel appointment|Try again)$/ })

/** A claim that the appointment is cancelled, anywhere a patient or a screen reader would meet it. */
const CLAIMS_CANCELLED = /\bcancelled\b/i

/** The server's own wording of a refusal, where it differs from the app's: never on screen. */
const NO_SERVER_TEXT = /RESOURCE_CONFLICT|BUSINESS_RULE_VIOLATION|server wording/

type User = ReturnType<typeof open>['user']

/** Open the dialog from the page's own button and choose a reason. */
async function chooseToCancel(user: User, why = 'I have a schedule conflict') {
  await user.click(await cancelButton())
  await dialog()
  await user.click(reason(why))
}

/** A 200 that is not this appointment, cancelled — built in the test from the appointment as it stands. */
type Unconfirmed = 'still-booked' | 'checked-in' | 'another' | 'no-times'

/** A request that got no answer in time, as Axios reports one. */
const timedOut: ScriptedAnswer = (config) => {
  throw new AxiosError('timeout of 30000ms exceeded', 'ECONNABORTED', config)
}

describe('an appointment — what is shown', () => {
  it('shows who with, where, when, the state, the reason and the reference — from one request', async () => {
    const { api } = setup([anAppointment({ reason: 'Chest pain\nsince Tuesday' })])
    open()

    expect(await title()).toBeInTheDocument()
    await waitFor(() => expect(document.title).toBe('Appointment · Atheris Health'))
    const region = await details()
    expect(valueOf(region, 'Status')).toHaveTextContent(/^Booked$/)
    expect(valueOf(region, 'Doctor')).toHaveTextContent(/^Asha MenonCardiology$/)
    expect(valueOf(region, 'Hospital')).toHaveTextContent(/^City Care$/)
    expect(within(region).getByRole('link', { name: 'City Care' })).toHaveAttribute('href', '/hospitals/city-care')
    expect(valueOf(region, 'Day')).toHaveTextContent(/^Monday 12 October 2026$/)
    expect(valueOf(region, 'Time')).toHaveTextContent(/^10:00 – 10:15$/)
    expect(within(region).getByText('Times are in the hospital’s local time (Asia/Kolkata)')).toBeInTheDocument()
    expect(valueOf(region, 'Reason for visit').textContent).toBe('Chest pain\nsince Tuesday')
    const reference = valueOf(region, 'Reference')
    expect(reference).toHaveTextContent(new RegExp(`^${REF}$`))
    expect(reference.className).toMatch(/font-mono/)
    expect(reference.className).toMatch(/select-all/)

    expect(screen.getByRole('link', { name: 'My appointments' })).toHaveAttribute('href', '/appointments')
    expect(live()).toBeEmptyDOMElement()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    // The appointment is all that is read; looking at it sends nothing else.
    expect(api.sent.map(routeOf)).toEqual([DETAIL])
    expect(api.sent[0].params).toBeUndefined()
  })

  it('an appointment with no reason and no specialisation shows neither, and invents nothing in their place', async () => {
    setup([anAppointment({ reason: null, doctor: { ref: doctorRef(1), name: 'Asha Menon' } })])
    open()

    const region = await details()
    expect(valueOf(region, 'Doctor')).toHaveTextContent(/^Asha Menon$/)
    expect(within(region).queryByText('Reason for visit')).not.toBeInTheDocument()
  })

  it.each([
    ['checked_in', 'Checked in'],
    ['in_progress', 'In progress'],
    ['completed', 'Completed'],
    ['cancelled', 'Cancelled'],
    ['no_show', 'Missed'],
  ])('%s is shown as "%s", with nothing to cancel and no advice to call the hospital', async (status, label) => {
    setup([anAppointment({ status })])
    open()

    expect(valueOf(await details(), 'Status')).toHaveTextContent(new RegExp(`^${label}$`))
    noCancelButton()
    expect(main()).not.toHaveTextContent(/contact the hospital|You can cancel until/)
    expect(main()).not.toHaveTextContent(/checked_in|in_progress|no_show/)
  })

  it('shows the day and the time on the HOSPITAL’s clock: Los Angeles, twelve and a half hours behind the test', async () => {
    setup([
      anAppointment({
        start: '2026-10-13T02:30:00Z',
        end: '2026-10-13T02:45:00Z',
        timezone: 'America/Los_Angeles',
      }),
    ])
    open()

    const region = await details()
    expect(valueOf(region, 'Day')).toHaveTextContent(/^Monday 12 October 2026$/)
    expect(valueOf(region, 'Time')).toHaveTextContent(/^19:30 – 19:45$/)
    expect(within(region).getByText('Times are in the hospital’s local time (America/Los_Angeles)')).toBeInTheDocument()
    // The cut-off is two hours before, on the same clock, with the zone named.
    expect(screen.getByText('You can cancel until 17:30, Monday 12 October 2026 (America/Los_Angeles)')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Tuesday|13 October|08:00|06:00|02:30|00:30/)
  })

  it('a hospital reference that could not be one path segment is shown as a name, not as a link', async () => {
    setup([anAppointment({ hospital: { ref: '../admin', name: 'City Care' } })])
    open()

    const region = await details()
    expect(valueOf(region, 'Hospital')).toHaveTextContent(/^City Care$/)
    expect(within(region).queryByRole('link')).not.toBeInTheDocument()
    expect(main().innerHTML).not.toContain('admin')
  })

  it('ATTACK — fields the contract does not name never reach the page', async () => {
    setup([anAppointment({ reason: 'Annual review' })], {
      shape: (appointment) => ({
        ...appointment,
        id: 'INTERNAL-ID-SECRET',
        patient_id: 'PATIENT-ID-SECRET',
        notes: 'NOTES-SECRET',
        cancelled_reason: 'CANCELLED-REASON-SECRET',
        created_by: 'CREATED-BY-SECRET',
        idempotency_key: 'IDEMPOTENCY-KEY-SECRET',
        type: 'TYPE-SECRET',
        hospital: { ...(appointment.hospital as object), hospital_id: 'NESTED-SECRET' },
        doctor: { ...(appointment.doctor as object), user_id: 'USER-SECRET' },
      }),
    })
    const { user } = open()

    await details()
    expect(document.documentElement.outerHTML).not.toMatch(/SECRET/)
    // The doctor's reference is never on the page either: only the appointment's own.
    expect(document.documentElement.outerHTML).not.toContain(doctorRef(1))
    await user.click(await cancelButton())
    await dialog()
    expect(document.documentElement.outerHTML).not.toMatch(/SECRET/)
  })

  it('copies exactly the fields the pages use, and nothing else', () => {
    const smuggled = {
      ...anAppointment({ reason: 'Annual review' }),
      can_cancel: true,
      cancel_until: '2026-10-12T08:00:00+05:30',
      patient_id: 'p',
      notes: 'n',
      cancelled_reason: 'c',
      created_by: 's',
      idempotency_key: 'k',
      hospital: { ref: 'city-care', name: 'City Care', hospital_id: 'h' },
      doctor: { ref: doctorRef(1), name: 'Asha Menon', specialization: 'Cardiology', user_id: 'u' },
    }

    expect(toMyAppointment(smuggled)).toEqual({
      ref: REF,
      status: 'booked',
      start: '2026-10-12T10:00:00+05:30',
      end: '2026-10-12T10:15:00+05:30',
      timezone: 'Asia/Kolkata',
      hospital: { ref: 'city-care', name: 'City Care' },
      doctor: { ref: doctorRef(1), name: 'Asha Menon', specialization: 'Cardiology' },
      reason: 'Annual review',
      can_cancel: true,
      cancel_until: '2026-10-12T08:00:00+05:30',
    })
  })

  it.each([
    ['the text "true"', 'true'],
    ['the number 1', 1],
    ['null', null],
    ['nothing', undefined],
  ])('can_cancel that is %s is not true; a cancel_until that is not an instant is not kept', (_case, value) => {
    const read = toMyAppointment({ ...anAppointment(), can_cancel: value, cancel_until: 'in two hours' })
    expect(read.can_cancel).toBe(false)
    expect(read.cancel_until).toBeNull()
  })

  it('ATTACK — markup in any field is text: shown as written, never run', async () => {
    setup([
      anAppointment({
        reason: '<img src=x onerror=window.pwned=1>',
        timezone: '<svg onload=window.pwned=1>',
        hospital: { ref: 'city-care', name: '<iframe src=javascript:alert(1)>' },
        doctor: { ref: doctorRef(1), name: '<script>window.pwned=1</script>', specialization: '<b>Cardiology</b>' },
      }),
    ])
    const { user } = open()

    const region = await details()
    expect(valueOf(region, 'Doctor').textContent).toBe('<script>window.pwned=1</script><b>Cardiology</b>')
    expect(valueOf(region, 'Hospital').textContent).toBe('<iframe src=javascript:alert(1)>')
    expect(valueOf(region, 'Reason for visit').textContent).toBe('<img src=x onerror=window.pwned=1>')
    await user.click(await cancelButton())
    expect(await dialog()).toHaveAccessibleDescription(/^<script>window\.pwned=1<\/script>, Monday 12 October 2026, 10:00 – 10:15 \(<svg onload=window\.pwned=1>\)$/)
    expect(document.body.querySelector('img, script, iframe, b, [onerror], [onload]')).toBeNull()
    expect((window as unknown as { pwned?: number }).pwned).toBeUndefined()
  })
})

describe('an appointment — "Cancel appointment" exists on the server’s word only', () => {
  it('the server says it can be cancelled: the button, and until when on the hospital’s clock', async () => {
    const { mine } = setup()
    open()

    expect(await cancelButton()).toBeEnabled()
    expect(mine.wireOf(REF)).toMatchObject({ can_cancel: true, cancel_until: '2026-10-12T08:00:00+05:30' })
    expect(screen.getByText('You can cancel until 08:00, Monday 12 October 2026 (Asia/Kolkata)')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/contact the hospital/)
    // Seeing the button opens nothing and sends nothing.
    noDialog()
    expect(mine.cancelRequests).toHaveLength(0)
  })

  it.each<[string, (appointment: WireAppointment) => WireAppointment]>([
    ['the text "true"', (appointment) => ({ ...appointment, can_cancel: 'true' })],
    ['the number 1', (appointment) => ({ ...appointment, can_cancel: 1 })],
    ['null', (appointment) => ({ ...appointment, can_cancel: null })],
    ['false', (appointment) => ({ ...appointment, can_cancel: false })],
    [
      'nothing at all',
      (appointment) => {
        const { can_cancel: _dropped, ...rest } = appointment
        return rest
      },
    ],
  ])('ATTACK — can_cancel that is %s: no button and no dialog, though it is BOOKED, days away, and the address asks', async (_case, shape) => {
    const { mine } = setup([anAppointment()], { shape })
    open(`${PATH}?cancel=1`)

    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)
    noCancelButton()
    noDialog()
    // Booked, and the app cannot cancel it: the patient is told where to turn.
    expect(screen.getByRole('note')).toHaveTextContent(
      /^This appointment can no longer be cancelled in the app\. Please contact the hospital\.$/,
    )
    // A "cancel until" is not shown for something that cannot be cancelled.
    expect(main()).not.toHaveTextContent(/You can cancel until/)
    expect(mine.cancelRequests).toHaveLength(0)
  })

  it('past the hospital’s cut-off: booked, no button, and where to turn instead', async () => {
    const { mine } = setup([anAppointment({ start: '2026-10-07T10:30:00+05:30', end: '2026-10-07T10:45:00+05:30' })])
    open()

    await details()
    expect(mine.wireOf(REF)).toMatchObject({ status: 'booked', can_cancel: false })
    noCancelButton()
    expect(screen.getByRole('note')).toHaveTextContent('This appointment can no longer be cancelled in the app. Please contact the hospital.')
  })

  it('a cancel_until that is not an instant is not shown; the button is still the server’s to give', async () => {
    setup([anAppointment()], { shape: (appointment) => ({ ...appointment, cancel_until: 'soon' }) })
    open()

    expect(await cancelButton()).toBeEnabled()
    expect(main()).not.toHaveTextContent(/You can cancel until|soon/)
  })
})

describe('an appointment — not found', () => {
  it('NOT FOUND (404) — unknown and someone else’s are one neutral page, with the way back', async () => {
    const other = myAppointmentRef(77)
    const { api } = setup([anAppointment()], { refs: [other] })
    open(`/appointments/${other}`)

    expect(await title('This appointment is not available')).toBeInTheDocument()
    expect(screen.getByText('It may not be one of your appointments, or the link you followed may be wrong.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to my appointments' })).toHaveAttribute('href', '/appointments')
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(main()).not.toHaveTextContent(other)
    expect(api.sent.map(routeOf)).toEqual([myAppointmentRoute(other)])
  })

  it.each([
    ['a number', '/appointments/17'],
    ['a word', '/appointments/next'],
    ['a UUID with a letter too many', `/appointments/${REF}0`],
    ['a UUID with something after it', `/appointments/${REF}%20OR%201=1`],
    ['an escaped slash', `/appointments/${REF}%2Fcancel`],
    ['a climb', '/appointments/..%2F..%2Fstaff'],
    ['markup', '/appointments/%3Cscript%3Ealert(1)%3C%2Fscript%3E'],
  ])('ATTACK — %s where the reference should be is the same page, and NOTHING is sent', async (_case, path) => {
    const { api } = setup()
    open(`${path}?cancel=1`)

    expect(await title('This appointment is not available')).toBeInTheDocument()
    noDialog()
    expect(api.sent).toHaveLength(0)
    expect(main().querySelector('script')).toBeNull()
  })

  it('an answer about ANOTHER appointment is not an answer: it is a failed read, and nothing of it is shown', async () => {
    const { mine } = setup()
    serve({ ...mine.routes, [DETAIL]: ok({ ...mine.wireOf(REF), ref: myAppointmentRef(2), doctor: { name: 'Someone Else' } }) })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load this appointment. Please try again.')
    expect(main()).not.toHaveTextContent('Someone Else')
    noCancelButton()
  })
})

describe('an appointment — when it cannot load', () => {
  afterEach(() => onlineManager.setOnline(true))

  it('ERROR — the app’s own message and a retry that works; nothing can be cancelled meanwhile', async () => {
    const { api, mine } = setup()
    api.on({ [DETAIL]: fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)') })
    const { user } = open(`${PATH}?cancel=1`)

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load this appointment. Please try again.')
    expect(document.body).not.toHaveTextContent('Traceback')
    noCancelButton()
    noDialog()

    api.on(mine.routes)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    // The address asked, and now the server says it can be cancelled: the question is put.
    expect(await dialog()).toBeInTheDocument()
    expect(api.calls(DETAIL)).toHaveLength(2)
  })

  it('OFFLINE — no answer is a connection problem; a 503 is not', async () => {
    const { api } = setup()
    api.on({ [DETAIL]: unreachable })
    open()
    expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
  })

  it('SERVER DOWN (503) is a failure on the server’s side', async () => {
    serve({ [DETAIL]: fail(503, 'SERVICE_UNAVAILABLE', 'upstream') })
    open()
    expect(await screen.findByRole('alert')).toHaveTextContent(/^We could not load this appointment\. Please try again\.$/)
    expect(document.body).not.toHaveTextContent(/No connection|upstream/)
  })

  it('OFFLINE — a request held back for lack of a network is not shown as loading for ever, and resumes by itself', async () => {
    onlineManager.setOnline(false)
    const { api } = setup()
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
    expect(api.sent).toHaveLength(0)
    act(() => onlineManager.setOnline(true))
    expect(await details()).toBeInTheDocument()
  })

  it.each([
    ['an appointment in a state the contract does not have', { status: 'rescheduled' }],
    ['an appointment with no times', { start: undefined, end: undefined }],
    ['an appointment that ends before it starts', { end: '2026-10-12T09:00:00+05:30' }],
    ['an appointment with no doctor', { doctor: null }],
  ])('MALFORMED — %s is a failed read, not a half-filled page', async (_case, broken) => {
    const { mine } = setup()
    serve({ ...mine.routes, [DETAIL]: ok({ ...mine.wireOf(REF), ...broken }) })
    open()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load this appointment. Please try again.')
    expect(screen.queryByRole('region', { name: 'Appointment details' })).not.toBeInTheDocument()
    noCancelButton()
  })
})

describe('cancelling — the question is asked first', () => {
  it('the button opens a modal dialog that restates the appointment, on the hospital’s clock, and sends nothing', async () => {
    const { api, mine } = setup()
    const { user, router } = open()

    await user.click(await cancelButton())

    const box = await dialog()
    expect(box).toHaveAttribute('aria-modal', 'true')
    expect(box).toHaveAccessibleDescription('Asha Menon, Monday 12 October 2026, 10:00 – 10:15 (Asia/Kolkata)')
    // Focus moves into it, to its title.
    expect(within(box).getByRole('heading', { level: 2, name: 'Cancel this appointment?' })).toHaveFocus()
    expect(router.state.location.search).toBe('?cancel=1')

    const group = within(box).getByRole('group', { name: 'Why are you cancelling?' })
    expect(within(group).getAllByRole('radio').map((radio) => (radio as HTMLInputElement).value)).toEqual([
      'schedule_conflict',
      'feeling_better',
      'booked_by_mistake',
      'other',
    ])
    expect(within(group).getAllByRole('radio').map((radio) => radio.closest('label')?.textContent)).toEqual([
      'I have a schedule conflict',
      'I am feeling better',
      'I booked by mistake',
      'Another reason',
    ])
    for (const radio of within(group).getAllByRole('radio')) expect(radio).not.toBeChecked()
    expect(detailsField()).toHaveValue('')
    expect(inDialog().getByText('0 of 200 characters')).toBeInTheDocument()
    expect(keepButton()).toBeEnabled()
    // No reason, no cancelling.
    expect(submitButton()).toBeDisabled()
    expect(submitButton()).toHaveAttribute('data-variant', 'destructive')

    expect(document.body).not.toHaveTextContent(CLAIMS_CANCELLED)
    expect(api.sent.map(routeOf)).toEqual([DETAIL])
    expect(mine.cancelRequests).toHaveLength(0)
  })

  it('"Keep appointment" closes it, sends nothing, and puts focus back on the button that opened it', async () => {
    const { mine } = setup()
    const { user, router } = open()
    await chooseToCancel(user)

    await user.click(keepButton())

    noDialog()
    expect(await cancelButton()).toHaveFocus()
    expect(router.state.location.search).toBe('')
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)
    expect(mine.cancelRequests).toHaveLength(0)
    expect(mine.wireOf(REF).status).toBe('booked')
  })

  it('Escape closes it the same way, from anywhere inside it', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    await user.type(detailsField(), 'Travelling')

    await user.keyboard('{Escape}')

    noDialog()
    expect(await cancelButton()).toHaveFocus()
    expect(mine.cancelRequests).toHaveLength(0)

    // Opened again, it starts again: nothing chosen, nothing typed.
    await user.click(await cancelButton())
    await dialog()
    expect(reason('I have a schedule conflict')).not.toBeChecked()
    expect(detailsField()).toHaveValue('')
    expect(submitButton()).toBeDisabled()
  })

  it('a press on the page behind it closes it too, and cancels nothing', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)

    fireEvent.mouseDown((await dialog()).parentElement!)

    noDialog()
    expect(mine.cancelRequests).toHaveLength(0)
  })

  it('the "Cancel" link of the list arrives with the question already put; keeping the appointment takes the request out of the address', async () => {
    const { mine } = setup()
    const { user, router } = open(`${PATH}?cancel=1`)

    expect(await dialog()).toBeInTheDocument()
    // Arriving is not confirming.
    expect(mine.cancelRequests).toHaveLength(0)
    expect(submitButton()).toBeDisabled()

    await user.click(keepButton())
    noDialog()
    expect(router.state.location.search).toBe('')
    // Nothing had focus worth returning to: it goes to the button that would open the dialog again.
    expect(await cancelButton()).toHaveFocus()
  })

  it('focus cannot leave the dialog: Tab and Shift+Tab go round inside it', async () => {
    setup()
    const { user } = open()
    await user.click(await cancelButton())
    const box = await dialog()
    const heading = within(box).getByRole('heading', { level: 2 })
    expect(heading).toHaveFocus()

    // With no reason chosen the destructive button is disabled, so "Keep appointment" is the last stop.
    await user.tab()
    expect(reason('I have a schedule conflict')).toHaveFocus()
    await user.tab()
    expect(detailsField()).toHaveFocus()
    await user.tab()
    expect(keepButton()).toHaveFocus()
    await user.tab()
    expect(reason('I have a schedule conflict')).toHaveFocus()
    await user.tab({ shift: true })
    expect(keepButton()).toHaveFocus()

    // With a reason chosen, the group is still one stop — the chosen one — and the last stop is the destructive button.
    await user.click(reason('I booked by mistake'))
    await user.tab({ shift: true })
    expect(submitButton()).toHaveFocus()
    await user.tab()
    expect(reason('I booked by mistake')).toHaveFocus()
    await user.tab({ shift: true })
    expect(submitButton()).toHaveFocus()

    // From the title, Shift+Tab goes to the end, not out to the page.
    heading.focus()
    await user.tab({ shift: true })
    expect(submitButton()).toHaveFocus()

    // Focus that gets out some other way is brought back.
    act(() => screen.getByRole('link', { name: 'My appointments' }).focus())
    expect(box).toContainElement(document.activeElement as HTMLElement)
  })

  it('Enter on a reason chooses nothing and cancels nothing: only the button confirms', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)

    reason('I have a schedule conflict').focus()
    await user.keyboard('{Enter}')
    await user.type(detailsField(), 'A line{Enter}and another')

    expect(await dialog()).toBeInTheDocument()
    expect(detailsField()).toHaveValue('A line\nand another')
    expect(mine.cancelRequests).toHaveLength(0)
  })
})

describe('cancelling — confirming', () => {
  it('sends ONE request: the reason code in the body, the appointment in the path, and nothing else', async () => {
    const { api, mine } = setup()
    const { user, router } = open()
    await chooseToCancel(user, 'I am feeling better')

    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(api.sent.map(routeOf)).toEqual([DETAIL, CANCEL])
    expect(mine.cancelRequests).toHaveLength(1)
    const [request] = mine.cancelRequests
    expect(request.body).toEqual({ reason_code: 'feeling_better' })
    expect(request.rawBody).toBe('{"reason_code":"feeling_better"}')
    expect(request.authorization).toBe('Bearer access-1')
    expect(Object.keys(api.calls(CANCEL)[0].params ?? {})).toEqual([])
    expect(api.calls(CANCEL)[0].headers.get('Idempotency-Key')).toBeUndefined()

    // The page now shows the server's answer.
    const region = await details()
    expect(valueOf(region, 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(live()).toHaveTextContent(/^Appointment cancelled$/)
    expect(valueOf(region, 'Status').firstElementChild).toHaveFocus()
    expect(screen.getByRole('note')).toHaveTextContent('Appointment cancelledYour appointment has been cancelled.')
    noCancelButton()
    expect(main()).not.toHaveTextContent(/You can cancel until|contact the hospital/)
    expect(router.state.location.search).toBe('')
    // The answer was enough: the appointment was not asked for again.
    expect(api.calls(DETAIL)).toHaveLength(1)
    expect(mine.cancellations).toEqual([{ ref: REF, reason_code: 'feeling_better', reason_text: null }])
  })

  it.each([
    ['I have a schedule conflict', 'schedule_conflict'],
    ['I am feeling better', 'feeling_better'],
    ['I booked by mistake', 'booked_by_mistake'],
    ['Another reason', 'other'],
  ])('"%s" is sent as %s', async (label, code) => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user, label)
    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(mine.cancelRequests.map((request) => request.body)).toEqual([{ reason_code: code }])
  })

  it('sends the details trimmed when some are typed — and the reason chosen last, not first', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user, 'I am feeling better')
    await user.click(reason('Another reason'))
    await user.type(detailsField(), '   My flight was moved to that morning.  ')
    expect(inDialog().getByText('36 of 200 characters')).toBeInTheDocument()

    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(mine.cancelRequests[0].body).toEqual({ reason_code: 'other', reason_text: 'My flight was moved to that morning.' })
    expect(mine.cancellations).toEqual([{ ref: REF, reason_code: 'other', reason_text: 'My flight was moved to that morning.' }])
  })

  it.each([
    ['nothing', ''],
    ['only spaces and new lines', '   \n  \n '],
  ])('sends no details at all when %s is typed', async (_case, typed) => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    if (typed) fireEvent.change(detailsField(), { target: { value: typed } })

    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(mine.cancelRequests[0].rawBody).toBe('{"reason_code":"schedule_conflict"}')
  })

  it('details over 200 characters are stopped in the dialog: nothing is sent until they fit', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    // Counted in characters, as the server counts: 200 emoji are 200, not 400.
    fireEvent.change(detailsField(), { target: { value: '🙂'.repeat(200) } })
    expect(inDialog().getByText('200 of 200 characters')).toBeInTheDocument()
    fireEvent.change(detailsField(), { target: { value: 'x'.repeat(201) } })
    expect(inDialog().getByText('201 of 200 characters')).toBeInTheDocument()

    await user.click(submitButton())

    expect(inDialog().getByRole('alert')).toHaveTextContent('Keep the details to 200 characters or fewer.')
    expect(detailsField()).toHaveFocus()
    expect(detailsField()).toHaveAttribute('aria-invalid', 'true')
    expect(mine.cancelRequests).toHaveLength(0)

    fireEvent.change(detailsField(), { target: { value: 'x'.repeat(200) } })
    await user.click(submitButton())
    await waitFor(() => noDialog())
    expect(mine.cancelRequests).toHaveLength(1)
    expect(mine.cancelRequests[0].body).toEqual({ reason_code: 'schedule_conflict', reason_text: 'x'.repeat(200) })
  })

  it('says "cancelled" only AFTER the server has: while the request is out, the dialog is busy and nothing claims it', async () => {
    const { mine } = setup()
    const answer = deferred()
    mine.next(answer.handler)
    const { user } = open()
    await chooseToCancel(user)

    await user.click(submitButton())

    await waitFor(() => expect(mine.cancelRequests).toHaveLength(1))
    expect(await dialog()).toHaveAttribute('aria-busy', 'true')
    expect(submitButton()).toBeDisabled()
    expect(submitButton()).toHaveAttribute('aria-busy', 'true')
    expect(keepButton()).toBeDisabled()
    expect(live()).toHaveTextContent(/^Cancelling your appointment…$/)
    // Not on the page, not in the dialog, not announced, not in the title.
    expect(document.body).not.toHaveTextContent(CLAIMS_CANCELLED)
    expect(document.title).not.toMatch(CLAIMS_CANCELLED)
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)
    // It cannot be walked away from by Escape or by a press outside while the answer is awaited.
    await user.keyboard('{Escape}')
    fireEvent.mouseDown((await dialog()).parentElement!)
    expect(await dialog()).toBeInTheDocument()

    await act(async () => answer.answer(ok({ ...mine.wireOf(REF), status: 'cancelled', can_cancel: false, cancel_until: null })))

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(live()).toHaveTextContent(/^Appointment cancelled$/)
    expect(mine.cancelRequests).toHaveLength(1)
  })

  it('shows the appointment from the SERVER’s answer, not from what the page had', async () => {
    const { api, mine } = setup()
    // The server's answer differs from what was on screen: the page must follow it.
    mine.next(
      ok({
        ...mine.wireOf(REF),
        status: 'cancelled',
        can_cancel: false,
        cancel_until: null,
        start: '2026-10-14T16:00:00+05:30',
        end: '2026-10-14T16:20:00+05:30',
        hospital: { ref: 'lakeside-clinic', name: 'Lakeside Clinic' },
        doctor: { ref: doctorRef(3), name: 'Meera Iyer', specialization: 'Joint Replacement' },
      }),
    )
    const { user } = open()
    await chooseToCancel(user)
    await user.click(submitButton())

    await waitFor(() => noDialog())
    const region = await details()
    expect(valueOf(region, 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(valueOf(region, 'Doctor')).toHaveTextContent(/^Meera IyerJoint Replacement$/)
    expect(within(region).getByRole('link', { name: 'Lakeside Clinic' })).toHaveAttribute('href', '/hospitals/lakeside-clinic')
    expect(valueOf(region, 'Day')).toHaveTextContent(/^Wednesday 14 October 2026$/)
    expect(valueOf(region, 'Time')).toHaveTextContent(/^16:00 – 16:20$/)
    expect(main()).not.toHaveTextContent(/Asha Menon|City Care/)
    expect(api.calls(DETAIL)).toHaveLength(1)
  })

  it.each<[string, (button: HTMLElement, user: User) => Promise<void>]>([
    ['a double click', async (button, user) => user.dblClick(button)],
    [
      'Enter and then a click',
      async (button, user) => {
        button.focus()
        await user.keyboard('{Enter}')
        await user.click(button)
      },
    ],
    [
      'ten rapid clicks',
      async (button) => {
        // Raw events, all before the page can redraw: `disabled` has not arrived to stop any of them.
        act(() => {
          for (let click = 0; click < 10; click++) fireEvent.click(button)
        })
      },
    ],
    [
      'ten submissions of the form itself',
      async (button) => {
        act(() => {
          for (let submission = 0; submission < 10; submission++) fireEvent.submit(button.closest('form')!)
        })
      },
    ],
  ])('ATTACK — %s cancels once: one request leaves, whatever the button looked like', async (_case, press) => {
    const { mine } = setup()
    const answer = deferred()
    mine.next(answer.handler)
    const { user } = open()
    await chooseToCancel(user)
    const button = submitButton()
    const form = button.closest('form')!

    await press(button, user)
    await waitFor(() => expect(mine.cancelRequests).toHaveLength(1))
    // Still out, and still pressed at.
    act(() => {
      fireEvent.click(button)
      fireEvent.submit(form)
    })
    await act(async () => answer.answer(ok({ ...mine.wireOf(REF), status: 'cancelled', can_cancel: false, cancel_until: null })))

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(mine.cancelRequests).toHaveLength(1)
  })

  it('an answer that arrives after the patient has left the page changes nothing where they are now', async () => {
    const { mine } = setup()
    const answer = deferred()
    mine.next(answer.handler)
    const { user, router } = open()
    await chooseToCancel(user)
    await user.click(submitButton())
    await waitFor(() => expect(mine.cancelRequests).toHaveLength(1))

    await act(() => router.navigate('/appointments?view=past'))
    await screen.findByRole('heading', { level: 1, name: 'My appointments' })
    await act(async () => answer.answer(ok({ ...mine.wireOf(REF), status: 'cancelled', can_cancel: false, cancel_until: null })))

    expect(router.state.location.pathname).toBe('/appointments')
    expect(router.state.location.search).toBe('?view=past')
    noDialog()
    expect(screen.getByRole('status')).not.toHaveTextContent('Appointment cancelled')
    expect(mine.cancelRequests).toHaveLength(1)
  })
})

describe('cancelling — refusals: the page says what happened and shows the appointment as the server now has it', () => {
  it('NO LONGER CANCELLABLE (409) — staff checked the patient in since the page loaded: the message, then the real status, and no button', async () => {
    const { api, mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    mine.change(REF, { status: 'checked_in' })
    const reread = deferred()
    api.on({ [DETAIL]: reread.handler })

    await user.click(submitButton())

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/^This appointment can no longer be cancelled\.$/)
    expect(alert).toHaveFocus()
    noDialog()
    // What the page knew is gone at once: no stale "Booked", and above all no stale button.
    expect(screen.queryByRole('region', { name: 'Appointment details' })).not.toBeInTheDocument()
    noCancelButton()
    expect(live()).toHaveTextContent(/^Loading appointment…$/)
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)

    await act(async () => reread.answer(ok(mine.wireOf(REF))))

    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Checked in$/)
    noCancelButton()
    // The message stays; the advice for a booked appointment does not apply to one that is checked in.
    expect(screen.getByRole('alert')).toHaveTextContent('This appointment can no longer be cancelled.')
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(api.calls(DETAIL)).toHaveLength(2)
    expect(mine.cancelRequests).toHaveLength(1)
    expect(mine.cancellations).toHaveLength(0)
    expect(document.body).not.toHaveTextContent(NO_SERVER_TEXT)
  })

  it.each([
    ['in progress', 'in_progress', 'In progress'],
    ['completed', 'completed', 'Completed'],
    ['a no-show', 'no_show', 'Missed'],
  ])('NO LONGER CANCELLABLE (409) — it is %s by now: the endpoint’s own refusal, and the page ends on "%s"', async (_case, status, label) => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    mine.change(REF, { status })

    await user.click(submitButton())

    expect(await screen.findByRole('alert')).toHaveTextContent('This appointment can no longer be cancelled.')
    expect(valueOf(await details(), 'Status')).toHaveTextContent(new RegExp(`^${label}$`))
    noCancelButton()
    noDialog()
  })

  it('TOO LATE (400) — the cut-off passed since the page loaded: the message, then the appointment still booked, and no button', async () => {
    const { api, mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    // Moved by the hospital to ninety minutes from now: inside the two hours it asks for.
    mine.change(REF, { start: '2026-10-07T10:30:00+05:30', end: '2026-10-07T10:45:00+05:30' })

    await user.click(submitButton())

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/^It is too late to cancel this appointment in the app\. Please contact the hospital\.$/)
    expect(alert).toHaveFocus()
    noDialog()

    const region = await details()
    expect(valueOf(region, 'Status')).toHaveTextContent(/^Booked$/)
    expect(valueOf(region, 'Day')).toHaveTextContent(/^Wednesday 7 October 2026$/)
    expect(valueOf(region, 'Time')).toHaveTextContent(/^10:30 – 10:45$/)
    noCancelButton()
    expect(main()).not.toHaveTextContent(/You can cancel until/)
    // Said once, by the refusal; not a second time by the standing advice.
    expect(screen.queryByRole('note')).not.toBeInTheDocument()
    expect(api.calls(DETAIL)).toHaveLength(2)
    expect(mine.cancellations).toHaveLength(0)
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)
  })

  it('a refusal whose re-read says it CAN be cancelled after all offers the button again, and the question starts afresh', async () => {
    const { mine } = setup()
    mine.next(cancelRefusals.noLongerCancellable())
    const { user } = open()
    await chooseToCancel(user)
    await user.click(submitButton())
    expect(await screen.findByRole('alert')).toHaveTextContent('This appointment can no longer be cancelled.')

    // The server is the one to say: it still says booked and cancellable.
    await user.click(await cancelButton())
    await dialog()
    expect(screen.queryByText('This appointment can no longer be cancelled.')).not.toBeInTheDocument()
    expect(submitButton()).toBeDisabled()
    await user.click(reason('Another reason'))
    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(mine.cancelRequests.map((request) => request.body)).toEqual([{ reason_code: 'schedule_conflict' }, { reason_code: 'other' }])
  })

  it('NOT FOUND (404) — the appointment is not there any more: the neutral page, with focus on what it says', async () => {
    const { mine } = setup()
    mine.next(cancelRefusals.notFound())
    const { user } = open()
    await chooseToCancel(user)

    await user.click(submitButton())

    expect(await title('This appointment is not available')).toHaveFocus()
    noDialog()
    expect(screen.getByRole('link', { name: 'Go to my appointments' })).toHaveAttribute('href', '/appointments')
    expect(main()).not.toHaveTextContent(/Asha Menon|Booked|Cancelled/)
    expect(mine.cancelRequests).toHaveLength(1)
  })

  it('INVALID (422) — a request the server could not read is not sent again as it was: the answers can be changed', async () => {
    const { mine } = setup()
    mine.next(cancelRefusals.invalid())
    const { user } = open()
    await chooseToCancel(user)

    await user.click(submitButton())

    const alert = await inDialog().findByRole('alert')
    expect(alert).toHaveTextContent('We could not send this requestCheck your answers and try again.')
    expect(reason('I have a schedule conflict')).toBeEnabled()
    expect(detailsField()).not.toHaveAttribute('readonly')
    expect(submitButton()).toHaveAccessibleName('Cancel appointment')
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)

    await user.click(reason('I booked by mistake'))
    await user.click(submitButton())
    await waitFor(() => noDialog())
    expect(mine.cancelRequests.map((request) => request.body)).toEqual([
      { reason_code: 'schedule_conflict' },
      { reason_code: 'booked_by_mistake' },
    ])
  })
})

describe('cancelling — an answer that did not arrive: the dialog stays, and the SAME request is sent again', () => {
  afterEach(() => onlineManager.setOnline(true))

  it.each<[string, ScriptedAnswer | Unconfirmed, string, string]>([
    ['OFFLINE — no network', unreachable, 'No connection', 'We could not reach the server. Your appointment has not been confirmed as cancelled. Check your internet connection and try again.'],
    ['OFFLINE — a timeout', timedOut, 'No connection', 'We could not reach the server. Your appointment has not been confirmed as cancelled. Check your internet connection and try again.'],
    ['SERVER — a 500', fail(500, 'INTERNAL_ERROR', 'server wording'), 'We could not cancel the appointment', 'Something went wrong on our side. Your appointment has not been confirmed as cancelled. Try again.'],
    ['SERVER — a 503', fail(503, 'SERVICE_UNAVAILABLE', 'server wording'), 'We could not cancel the appointment', 'Something went wrong on our side. Your appointment has not been confirmed as cancelled. Try again.'],
    ['SERVER — a pending policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), 'We could not cancel the appointment', 'Something went wrong on our side. Your appointment has not been confirmed as cancelled. Try again.'],
    ['UNCONFIRMED — a 200 that says it is still BOOKED', 'still-booked', 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
    ['UNCONFIRMED — a 200 that says it is CHECKED IN', 'checked-in', 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
    ['UNCONFIRMED — a 200 about another appointment', 'another', 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
    ['UNCONFIRMED — a 200 with an empty object', ok({}), 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
    ['UNCONFIRMED — a 204 with no body', noContent(), 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
    ['UNCONFIRMED — a 200 whose appointment has no times', 'no-times', 'We could not confirm the cancellation', 'Try again — it is safe to ask more than once.'],
  ])('%s: an error in the dialog, nothing claimed, and "Try again" sends the same body', async (_case, scripted, heading, body) => {
    const { api, mine } = setup()
    const booked = mine.wireOf(REF)
    const cancelledNow = { ...booked, status: 'cancelled', can_cancel: false, cancel_until: null }
    const unconfirmed: Record<Unconfirmed, ScriptedAnswer> = {
      'still-booked': ok(booked),
      'checked-in': ok({ ...booked, status: 'checked_in', can_cancel: false }),
      another: ok({ ...cancelledNow, ref: myAppointmentRef(2) }),
      'no-times': ok({ ...cancelledNow, start: null, end: null }),
    }
    const answer = typeof scripted === 'string' && scripted !== LOST ? unconfirmed[scripted] : scripted
    mine.next(answer)
    const { user } = open()
    await chooseToCancel(user, 'I booked by mistake')
    await user.type(detailsField(), 'Wrong doctor')

    await user.click(submitButton())

    const alert = await inDialog().findByRole('alert')
    expect(alert).toHaveTextContent(`${heading}${body}`)
    expect(alert).toHaveFocus()
    // The dialog is still open, on the same answers, which are now part of the request.
    expect(await dialog()).toBeInTheDocument()
    expect(reason('I booked by mistake')).toBeChecked()
    expect(reason('I booked by mistake')).toBeDisabled()
    expect(detailsField()).toHaveAttribute('readonly')
    expect(inDialog().getByText('Your answers cannot be changed while this request is being tried again.')).toBeInTheDocument()
    expect(keepButton()).toBeEnabled()
    // Nothing says it is cancelled: not the page behind, not the announcement.
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)
    expect(live()).toBeEmptyDOMElement()
    expect(document.body).not.toHaveTextContent(NO_SERVER_TEXT)
    expect(api.calls(DETAIL)).toHaveLength(1)

    const retry = submitButton()
    expect(retry).toHaveAccessibleName('Try again')
    await user.click(retry)

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(live()).toHaveTextContent(/^Appointment cancelled$/)
    expect(mine.cancelRequests).toHaveLength(2)
    const [first, second] = mine.cancelRequests
    expect(first.body).toEqual({ reason_code: 'booked_by_mistake', reason_text: 'Wrong doctor' })
    expect(second.rawBody).toBe(first.rawBody)
    expect(mine.cancellations).toHaveLength(1)
  })

  it('OFFLINE — when the browser says it has no network the request fails at once; it is not kept to be sent later', async () => {
    const { mine } = setup()
    const { user } = open()
    await chooseToCancel(user)
    onlineManager.setOnline(false)
    mine.next(unreachable)

    await user.click(submitButton())

    expect(await inDialog().findByRole('alert')).toHaveTextContent('No connection')
    expect(mine.cancelRequests).toHaveLength(1)
    act(() => onlineManager.setOnline(true))
    // Coming back online sends nothing behind the patient's back.
    await act(async () => {
      await Promise.resolve()
    })
    expect(mine.cancelRequests).toHaveLength(1)
    expect(mine.wireOf(REF).status).toBe('booked')
  })

  it('a cancellation the server made but whose answer was LOST: the retry gets the cancelled appointment back, and it is cancelled once', async () => {
    const { mine } = setup()
    mine.next(LOST)
    const { user } = open()
    await chooseToCancel(user)

    await user.click(submitButton())

    expect(await inDialog().findByRole('alert')).toHaveTextContent('No connection')
    // It IS cancelled on the server — and the page, which has not been told, does not say so.
    expect(mine.wireOf(REF).status).toBe('cancelled')
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)

    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(live()).toHaveTextContent(/^Appointment cancelled$/)
    expect(mine.cancelRequests).toHaveLength(2)
    expect(mine.cancelRequests[1].rawBody).toBe(mine.cancelRequests[0].rawBody)
    // The repeat was answered with the same appointment, and wrote no second history.
    expect(mine.cancellations).toHaveLength(1)
  })

  it('keeps the same request through any number of failures, each announced afresh, and "Keep appointment" still walks away', async () => {
    const { mine } = setup()
    mine.next(unreachable, fail(503, 'SERVICE_UNAVAILABLE'), timedOut)
    const { user } = open()
    await chooseToCancel(user, 'Another reason')
    await user.type(detailsField(), 'Plans changed')

    await user.click(submitButton())
    expect(await inDialog().findByText('No connection')).toBeInTheDocument()
    await user.click(submitButton())
    expect(await inDialog().findByText('We could not cancel the appointment')).toBeInTheDocument()
    expect(inDialog().getByRole('alert')).toHaveFocus()
    await user.click(submitButton())
    expect(await inDialog().findByText('No connection')).toBeInTheDocument()

    expect(mine.cancelRequests).toHaveLength(3)
    expect(new Set(mine.cancelRequests.map((request) => request.rawBody))).toEqual(
      new Set(['{"reason_code":"other","reason_text":"Plans changed"}']),
    )

    await user.click(keepButton())
    noDialog()
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)
    expect(mine.cancelRequests).toHaveLength(3)
  })

  it('a retry that is refused ends there: the refusal is shown and the appointment is read again', async () => {
    const { api, mine } = setup()
    mine.next(unreachable)
    const { user } = open()
    await chooseToCancel(user)
    await user.click(submitButton())
    await inDialog().findByText('No connection')
    mine.change(REF, { status: 'completed' })

    await user.click(submitButton())

    expect(await screen.findByRole('alert')).toHaveTextContent(/^This appointment can no longer be cancelled\.$/)
    noDialog()
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Completed$/)
    expect(api.calls(DETAIL)).toHaveLength(2)
  })
})

describe('cancelling — the session', () => {
  it('an expired session is refreshed and the SAME request is sent again: same body, new token', async () => {
    const { api, mine } = setup([anAppointment()], {}, { [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }) })
    mine.next(fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'))
    const { user } = open()
    await chooseToCancel(user, 'I am feeling better')
    await user.type(detailsField(), 'All better')

    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(api.calls(REFRESH)).toHaveLength(1)
    expect(mine.cancelRequests).toHaveLength(2)
    const [refused, retried] = mine.cancelRequests
    expect(refused.authorization).toBe('Bearer access-1')
    expect(retried.authorization).toBe('Bearer access-new')
    expect(retried.rawBody).toBe(refused.rawBody)
    expect(retried.body).toEqual({ reason_code: 'feeling_better', reason_text: 'All better' })
    expect(mine.cancellations).toHaveLength(1)
    // The patient saw one outcome, and no failure on the way to it.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('server wording')
  })

  it('a session that cannot be refreshed ends at the sign-in page, with nothing claimed', async () => {
    const { mine } = setup([anAppointment()], {}, { [REFRESH]: fail(401, 'UNAUTHORIZED') })
    mine.next(fail(401, 'AUTHENTICATION_REQUIRED'))
    const { user, router } = open()
    await chooseToCancel(user)

    await user.click(submitButton())

    expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(isSignedIn()).toBe(false)
    noDialog()
    expect(document.documentElement).not.toHaveClass('overflow-hidden')
    expect(mine.cancelRequests).toHaveLength(1)
    expect(document.body).not.toHaveTextContent(/Appointment cancelled/)
    expect(mine.wireOf(REF).status).toBe('booked')
  })
})

describe('cancelling — what else is no longer to be trusted afterwards', () => {
  it('the lists are read again: the appointment leaves "Upcoming" and is in "Past", cancelled, with nothing to cancel', async () => {
    const { api, mine } = setup()
    const { user, router } = open('/appointments')

    // From the list: the card's "Cancel" link leads to the question; it cancels nothing by itself.
    await user.click(await screen.findByRole('link', { name: /^Cancel Asha Menon/ }))
    await dialog()
    expect(router.state.location.pathname).toBe(PATH)
    expect(mine.cancelRequests).toHaveLength(0)
    await user.click(reason('I have a schedule conflict'))
    await user.click(submitButton())
    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    const listReadsBefore = api.calls(LIST).length

    await user.click(screen.getByRole('link', { name: 'My appointments' }))

    // Not shown from memory while the new list is on its way: it is asked for, and the answer has no such appointment.
    expect(await screen.findByText('When you book an appointment, it will be listed here.')).toBeInTheDocument()
    expect(screen.getByRole('main')).not.toHaveTextContent('Asha Menon')
    expect(api.calls(LIST).length).toBe(listReadsBefore + 1)
    expect(api.calls(LIST).at(-1)?.params).toEqual({ scope: 'upcoming', page: 1, page_size: 20 })

    await user.click(screen.getByRole('tab', { name: 'Past' }))
    const past = (await screen.findByRole('heading', { level: 3, name: 'Asha Menon' })).closest('li')!
    expect(within(past).getByText('Cancelled')).toBeInTheDocument()
    expect(within(past).queryByRole('link', { name: /cancel/i })).not.toBeInTheDocument()
  })

  it('a refusal empties the lists too: what they showed was as stale as the page', async () => {
    const { api, mine } = setup()
    const { user } = open('/appointments')
    await user.click(await screen.findByRole('link', { name: /^Cancel Asha Menon/ }))
    await dialog()
    await user.click(reason('I have a schedule conflict'))
    mine.change(REF, { status: 'completed' })
    await user.click(submitButton())
    await screen.findByRole('alert')
    const listReadsBefore = api.calls(LIST).length

    await user.click(screen.getByRole('link', { name: 'My appointments' }))

    expect(await screen.findByText('When you book an appointment, it will be listed here.')).toBeInTheDocument()
    expect(api.calls(LIST).length).toBe(listReadsBefore + 1)
  })

  it('the doctor’s availability is read again: the slot the appointment held may be free', async () => {
    const appointment = anAppointment({
      hospital: { ref: cityCareHospital.ref, name: cityCareHospital.name },
      doctor: { ref: ashaRao.ref, name: ashaRao.name, specialization: ashaRao.specialization },
    })
    const AVAILABILITY = `GET /hospitals/city-care/doctors/${ashaRao.ref}/availability`
    const { api } = setup([appointment], {}, {
      'GET /hospitals/city-care': ok(cityCareHospital),
      ...doctorDirectory('city-care', [ashaRao]),
    })
    signIn('access-1')
    const { user, router } = renderApp(`/hospitals/city-care/doctors/${ashaRao.ref}/availability`)
    await screen.findByRole('heading', { level: 1, name: 'Availability' })
    await waitFor(() => expect(api.calls(AVAILABILITY)).toHaveLength(1))

    await act(() => router.navigate(PATH))
    await chooseToCancel(user)
    await user.click(submitButton())
    await waitFor(() => noDialog())
    await details()

    await act(() => router.navigate(-1))
    await screen.findByRole('heading', { level: 1, name: 'Availability' })
    await waitFor(() => expect(api.calls(AVAILABILITY).length).toBeGreaterThan(1))
  })
})

describe('cancelling — the whole way there', () => {
  it('home → my appointments → the appointment → the question → cancelled, with one request to cancel and none before it', async () => {
    const { api, mine } = setup([anAppointment()], {}, { 'GET /me': ok(me()) })
    signIn('access-1')
    const { user, router } = renderApp('/')

    await user.click(await screen.findByRole('link', { name: 'My appointments' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'My appointments' })).toHaveFocus()
    await user.click(await screen.findByRole('link', { name: /^View appointment Asha Menon/ }))
    expect(await title()).toHaveFocus()
    expect(router.state.location.pathname).toBe(PATH)
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Booked$/)

    await chooseToCancel(user, 'I booked by mistake')
    // Everything so far was reading.
    expect(api.sent.map(routeOf)).toEqual(['GET /me', LIST, DETAIL])
    await user.click(submitButton())

    await waitFor(() => noDialog())
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(live()).toHaveTextContent(/^Appointment cancelled$/)
    expect(api.sent.map(routeOf)).toEqual(['GET /me', LIST, DETAIL, CANCEL])
    expect(mine.cancellations).toEqual([{ ref: REF, reason_code: 'booked_by_mistake', reason_text: null }])

    // Back and Forward do not ask the question again, and send nothing more to cancel.
    await act(() => router.navigate(-1))
    await screen.findByRole('heading', { level: 1, name: 'My appointments' })
    await act(() => router.navigate(1))
    expect(valueOf(await details(), 'Status')).toHaveTextContent(/^Cancelled$/)
    noDialog()
    noCancelButton()
    expect(mine.cancelRequests).toHaveLength(1)
  })
})

describe('cancelling — keyboard and assistive technology', () => {
  it('works from the keyboard alone: the list, the appointment, the question, a reason, details, confirm, and on from the outcome', async () => {
    const { mine } = setup()
    const { user, router } = open('/appointments')
    const tabTo = async (target: () => HTMLElement) => {
      for (let presses = 0; presses < 40 && document.activeElement !== target(); presses++) await user.tab()
      expect(target()).toHaveFocus()
    }

    await screen.findByRole('heading', { level: 3, name: 'Asha Menon' })
    await tabTo(() => screen.getByRole('link', { name: /^View appointment Asha Menon/ }))
    await user.keyboard('{Enter}')

    expect(await title()).toHaveFocus()
    expect(router.state.location.pathname).toBe(PATH)
    await tabTo(() => within(main()).getByRole('button', { name: 'Cancel appointment' }))
    await user.keyboard('{Enter}')

    const box = await dialog()
    expect(within(box).getByRole('heading', { level: 2 })).toHaveFocus()
    // The reasons are the first stop; Space chooses the one in focus, the arrows move the choice.
    await user.tab()
    expect(reason('I have a schedule conflict')).toHaveFocus()
    expect(submitButton()).toBeDisabled()
    await user.keyboard(' ')
    expect(reason('I have a schedule conflict')).toBeChecked()
    await user.keyboard('{ArrowDown}')
    expect(reason('I am feeling better')).toBeChecked()
    expect(mine.cancelRequests).toHaveLength(0)
    await user.tab()
    expect(detailsField()).toHaveFocus()
    await user.keyboard('The cough has gone')
    await user.tab()
    expect(keepButton()).toHaveFocus()
    await user.tab()
    expect(submitButton()).toHaveFocus()
    await user.keyboard('{Enter}')

    await waitFor(() => noDialog())
    const region = await details()
    expect(valueOf(region, 'Status')).toHaveTextContent(/^Cancelled$/)
    expect(valueOf(region, 'Status').firstElementChild).toHaveFocus()
    expect(mine.cancelRequests.map((request) => request.body)).toEqual([
      { reason_code: 'feeling_better', reason_text: 'The cough has gone' },
    ])

    // On from the outcome: the hospital, and back up to the list.
    await user.tab()
    expect(screen.getByRole('link', { name: 'City Care' })).toHaveFocus()
    await user.tab({ shift: true })
    expect(screen.getByRole('link', { name: 'My appointments' })).toHaveFocus()
  })

  it('in every state: one h1, one live region that stays, every control named and 44 px, icons hidden, nothing jumps the tab order', async () => {
    const { mine } = setup()
    mine.next(unreachable)
    const { user } = open()
    const sound = () => {
      const touchSized = /(^|\s)(h-11|min-h-11|min-h-20|size-11)(\s|$)/
      const controls = [...document.body.querySelectorAll<HTMLElement>('main a, main button, [role="dialog"] button, [role="dialog"] textarea')]
      for (const control of controls) expect(control.className).toMatch(touchSized)
      for (const control of controls) expect(control).toHaveAccessibleName()
      for (const radio of document.body.querySelectorAll('input[type="radio"]')) {
        expect(radio).toHaveAccessibleName()
        expect(radio.closest('label')?.className).toMatch(touchSized)
      }
      for (const element of document.querySelectorAll('[tabindex]')) expect(element).toHaveAttribute('tabindex', '-1')
      for (const icon of document.querySelectorAll('svg')) expect(icon).toHaveAttribute('aria-hidden', 'true')
      expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
      expect(screen.getAllByRole('status')).toHaveLength(1)
      expect(main().querySelector('table, img, time, form, input, select, textarea')).toBeNull()
    }

    await title()
    const region = live()
    await details()
    sound()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()

    await user.click(await cancelButton())
    await dialog()
    sound()
    // While the question is up, the page under it does not scroll.
    expect(document.documentElement).toHaveClass('overflow-hidden')
    await user.click(reason('I have a schedule conflict'))
    await user.click(submitButton())
    await inDialog().findByText('No connection')
    sound()
    await user.click(submitButton())
    await waitFor(() => noDialog())
    await details()
    sound()
    expect(document.documentElement).not.toHaveClass('overflow-hidden')

    // The same node throughout, so each change is announced; a replaced node would not be.
    expect(live()).toBe(region)
    expect(region).toHaveTextContent(/^Appointment cancelled$/)
  })
})

describe('cancelling — telling the failures apart', () => {
  const answered = (status: number, code: string, message = 'server wording') => new ApiError(message, code, status)

  it.each([
    ['a conflict (409)', answered(409, 'RESOURCE_CONFLICT'), 'no_longer_cancellable'],
    ['a conflict with any other code', answered(409, 'SOMETHING_ELSE'), 'no_longer_cancellable'],
    ['a business rule (400)', answered(400, 'BUSINESS_RULE_VIOLATION'), 'too_late'],
    ['a 400 with any other code', answered(400, 'BAD_REQUEST'), 'too_late'],
    ['not found (404)', answered(404, 'RESOURCE_NOT_FOUND'), 'not_found'],
    ['a body the server could not read (422)', answered(422, 'VALIDATION_ERROR'), 'invalid'],
    ['a pending policy (403)', answered(403, 'CONSENT_REQUIRED'), 'server'],
    ['the rate limit (429)', answered(429, 'RATE_LIMITED'), 'server'],
    ['a server error (500)', answered(500, 'INTERNAL_ERROR'), 'server'],
    ['a server that is down (503)', answered(503, 'SERVICE_UNAVAILABLE'), 'server'],
    ['a 2xx that is not the appointment, cancelled', new ApiError('Response is not an appointment', 'bad_response'), 'unconfirmed'],
    ['no answer at all', new ApiError('Network Error', 'network_error'), 'offline'],
    ['something that is not an answer', new Error('boom'), 'server'],
    ['an error of unknown kind', new ApiError('?'), 'server'],
  ])('%s is %s', (_case, error, kind) => {
    expect(classifyCancelError(error)).toBe(kind)
  })

  it('only a lost or unreadable answer is asked for again; a refusal never is', () => {
    expect((['offline', 'server', 'unconfirmed'] as const).every(isCancelRetriable)).toBe(true)
    expect((['no_longer_cancellable', 'too_late', 'not_found', 'invalid'] as const).some(isCancelRetriable)).toBe(false)
  })
})
