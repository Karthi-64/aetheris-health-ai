import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { fail, noContent, ok, serve } from '@/test/fakeApi'
import { appointmentRef, bookingEndpoint, LOST } from '@/test/booking'
import { doctorDirectory } from '@/test/doctorDirectory'
import {
  ashaRao,
  cityCare,
  lakesideHospital,
  manyHospitals,
  me,
  meeraIyer,
  otpRequested,
  PHONE_MASKED,
  PHONE_TYPED,
  verifiedSession,
} from '@/test/fixtures'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { myAppointmentsEndpoints } from '@/test/myAppointments'
import { loadApp } from '@/test/renderApp'

/**
 * ATTACK: a script injected into the page (or anyone with the device) reads
 * web storage looking for a token, a code or a phone number.
 * OUTCOME: there is nothing to find — the app neither writes nor reads it.
 * (`src/test/setup.ts` also asserts this after every other test, and
 * `src/test/node/storageSource.test.ts` checks the source itself.)
 */
const ROUTER_TRANSITIONS_KEY = 'remix-router-transitions'

describe('web storage', () => {
  it('is never touched across a whole session: load, sign in, use, sign out', async () => {
    const read = vi.spyOn(Storage.prototype, 'getItem')
    const touched = [
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
      vi.spyOn(Storage.prototype, 'key'),
    ]
    serve({
      'POST /auth/refresh': fail(401, 'UNAUTHORIZED'),
      'POST /auth/otp/request': ok(otpRequested(), 202),
      'POST /auth/otp/verify': ok(verifiedSession('access-secret')),
      'GET /me': ok(me([cityCare])),
      'POST /auth/logout': noContent(),
    })
    const { user } = loadApp('/')

    await user.type(await screen.findByLabelText('Mobile number'), PHONE_TYPED)
    await user.click(screen.getByRole('button', { name: 'Send code' }))
    await user.type(await screen.findByLabelText('6-digit code'), '482913')
    await user.click(screen.getByRole('button', { name: 'Verify and continue' }))
    expect(await screen.findByText(PHONE_MASKED)).toBeInTheDocument()

    // Signed in, with a token in memory — and nowhere else.
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
    expect(document.cookie).toBe('')
    expect(window.history.state).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByLabelText('Mobile number')

    for (const access of touched) expect(access).not.toHaveBeenCalled()
    // The one read is React Router's own, at start-up: it looks for view
    // transitions it saved before a reload. The app uses none, so the router
    // never writes that key (a write would fail the check in `setup.ts`), and
    // it holds route paths, not data. Nothing else is ever read.
    expect(read.mock.calls).toEqual([[ROUTER_TRANSITIONS_KEY]])
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
  })

  it('is never touched by discovery, booking and the patient’s appointments: search, filter, pages, a hospital, its doctors, a doctor, availability, a booking, the appointments, one of them, cancelling it, linking', async () => {
    const read = vi.spyOn(Storage.prototype, 'getItem')
    const touched = [
      vi.spyOn(Storage.prototype, 'removeItem'),
      vi.spyOn(Storage.prototype, 'clear'),
      vi.spyOn(Storage.prototype, 'key'),
    ]
    const booking = bookingEndpoint(lakesideHospital, ashaRao)
    // The patient's appointments are whatever that endpoint has booked.
    const mine = myAppointmentsEndpoints(booking.appointments, { refs: [appointmentRef(1)] })
    serve({
      'POST /auth/refresh': ok({ access_token: 'access-secret', expires_in: 900 }),
      'GET /me': ok(me([cityCare])),
      ...hospitalDirectory([lakesideHospital, ...manyHospitals(45)]),
      ...doctorDirectory('lakeside-clinic', [ashaRao, meeraIyer]),
      ...booking.routes,
      ...mine.routes,
    })
    // A reload in the middle of a search: the filters come from the URL, not from storage.
    const { user } = loadApp('/hospitals?q=clinic&city=Bengaluru')

    expect(await screen.findByRole('link', { name: 'Clinic 01' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByRole('link', { name: 'Clinic 21' })).toBeInTheDocument()
    await user.selectOptions(screen.getByRole('combobox', { name: 'City' }), 'Mysuru')
    await user.click(screen.getByRole('button', { name: 'Clear search' }))
    await user.click(await screen.findByRole('link', { name: 'Lakeside Clinic' }))
    await user.click(await screen.findByRole('link', { name: 'View Doctors' }))
    await screen.findByRole('heading', { name: 'Doctors at Lakeside Clinic' })
    // A search and a department filter live in the URL too, and a doctor's pages keep nothing either.
    await user.type(screen.getByRole('searchbox', { name: 'Search by name or specialisation' }), 'rao{Enter}')
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Department' }), 'Cardiology')
    await user.click(await screen.findByRole('link', { name: 'View Profile Asha Rao' }))
    await user.click(await screen.findByRole('link', { name: 'View Availability' }))
    await screen.findByRole('heading', { name: 'Availability' })
    // The day, the week and the slot live in the URL too, and so does the slot handed on to booking.
    await user.click(await screen.findByRole('button', { name: 'Friday 9 October 2026, 2 free slots' }))
    await user.click(screen.getByRole('button', { name: /^10:15 to 10:30, Friday 9 October 2026$/ }))
    await user.click(screen.getByRole('button', { name: 'Later days' }))
    await screen.findByRole('button', { name: /^Wednesday 14 October 2026/ })
    await user.click(screen.getByRole('button', { name: 'Earlier days' }))
    await user.click(await screen.findByRole('button', { name: /^Friday 9 October 2026/ }))
    await user.click(screen.getByRole('button', { name: /^10:00 to 10:15, Friday 9 October 2026$/ }))
    await user.click(screen.getByRole('link', { name: 'Continue to booking' }))
    await screen.findByRole('heading', { name: 'Booking' })
    await user.click(screen.getByRole('link', { name: 'Back to availability' }))
    await screen.findByRole('heading', { name: 'Availability' })
    // The booking itself: the reason, the idempotency key and the confirmation live in memory only.
    await user.click(await screen.findByRole('link', { name: 'Continue to booking' }))
    await screen.findByRole('heading', { name: 'Booking' })
    booking.next(LOST)
    await user.type(screen.getByRole('textbox', { name: 'Reason for visit (optional)' }), 'Chest pain')
    await user.click(screen.getByRole('button', { name: 'Confirm appointment' }))
    await user.click(await screen.findByRole('button', { name: 'Try again' }))
    await screen.findByRole('heading', { name: 'Appointment booked' })
    expect(booking.requests).toHaveLength(2)
    expect(booking.requests[1].key).toBe(booking.requests[0].key)
    expect(window.history.state).toBeNull()
    await user.click(screen.getByRole('link', { name: 'Back to the doctor' }))
    await user.click(await screen.findByRole('link', { name: 'Doctors at Lakeside Clinic' }))
    await user.click(await screen.findByRole('link', { name: 'Back to Lakeside Clinic' }))
    // On to the patient's own appointments: the two views and the page live in the URL, and so does
    // the request to cancel; the reason, the details and the outcome live in memory only.
    await user.click(screen.getByRole('link', { name: 'Appointments' }))
    await screen.findByRole('heading', { level: 1, name: 'My appointments' })
    await screen.findByRole('heading', { level: 3, name: 'Asha Rao' })
    await user.click(screen.getByRole('tab', { name: 'Past' }))
    await screen.findByText('No past appointments', { selector: 'p.font-display' })
    await user.click(screen.getByRole('tab', { name: 'Upcoming' }))
    await user.click(await screen.findByRole('link', { name: /^View appointment Asha Rao/ }))
    await screen.findByRole('region', { name: 'Appointment details' })
    await user.click(screen.getByRole('button', { name: 'Cancel appointment' }))
    await user.click(await screen.findByRole('button', { name: 'Keep appointment' }))
    await user.click(screen.getByRole('link', { name: 'My appointments' }))
    await user.click(await screen.findByRole('link', { name: /^Cancel Asha Rao/ }))
    const question = within(await screen.findByRole('dialog', { name: 'Cancel this appointment?' }))
    await user.click(question.getByRole('radio', { name: 'Another reason' }))
    await user.type(question.getByRole('textbox', { name: 'More details (optional)' }), 'Feeling better')
    mine.next(LOST)
    await user.click(question.getByRole('button', { name: 'Cancel appointment' }))
    await user.click(await question.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Appointment cancelled'))
    expect(mine.cancelRequests).toHaveLength(2)
    expect(mine.cancelRequests[1].rawBody).toBe(mine.cancelRequests[0].rawBody)
    expect(mine.cancellations).toHaveLength(1)
    expect(window.history.state).toBeNull()
    await user.click(screen.getByRole('link', { name: 'Lakeside Clinic' }))
    await user.click(await screen.findByRole('link', { name: 'Link my record' }))
    expect(await screen.findByLabelText('Hospital code')).toHaveValue('lakeside-clinic')

    for (const access of touched) expect(access).not.toHaveBeenCalled()
    expect(read.mock.calls).toEqual([[ROUTER_TRANSITIONS_KEY]])
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
    expect(document.cookie).toBe('')
  })
})
