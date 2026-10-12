import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { HospitalLink } from '@/api/me'
import { ok, serve } from '@/test/fakeApi'
import { cityCare, cityCareHospital, lakeside, me } from '@/test/fixtures'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { anAppointment, myAppointmentRef, myAppointmentsEndpoints } from '@/test/myAppointments'
import { renderApp, signIn } from '@/test/renderApp'

const ME = 'GET /me'

describe('home — the way into hospital discovery', () => {
  it('offers "Find a hospital", which opens the list', async () => {
    signIn()
    const api = serve({ [ME]: ok(me()), ...hospitalDirectory([cityCareHospital]) })
    const { user, router } = renderApp('/')
    await screen.findByRole('heading', { name: 'Welcome' })

    // Home itself asks only for the account: the list is fetched when it is opened.
    // (The heading is on screen before the request for the account has left.)
    await screen.findByText('No hospital linked yet')
    expect(api.sent.map((request) => request.url)).toEqual(['/me'])

    await user.click(screen.getByRole('link', { name: 'Find a hospital' }))

    expect(await screen.findByRole('heading', { level: 1, name: 'Find a hospital' })).toHaveFocus()
    expect(router.state.location.pathname).toBe('/hospitals')
    expect(await screen.findByRole('link', { name: 'City Care Hospital' })).toBeInTheDocument()
  })

  it('has "Hospitals" in the navigation, current only on the hospital pages', async () => {
    signIn()
    serve({ [ME]: ok(me()), ...hospitalDirectory([cityCareHospital]) })
    const { user, router } = renderApp('/')
    await screen.findByRole('heading', { name: 'Welcome' })

    const navigation = within(screen.getByRole('navigation', { name: 'Main' }))
    expect(navigation.getAllByRole('link').map((link) => link.textContent)).toEqual(['Home', 'Hospitals', 'Appointments', 'Link hospital'])
    expect(navigation.getByRole('link', { name: 'Home' })).toHaveAttribute('aria-current', 'page')
    expect(navigation.getByRole('link', { name: 'Hospitals' })).not.toHaveAttribute('aria-current')

    await user.click(navigation.getByRole('link', { name: 'Hospitals' }))

    expect(await screen.findByRole('heading', { level: 1, name: 'Find a hospital' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/hospitals')
    expect(navigation.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')
    expect(navigation.getByRole('link', { name: 'Home' })).not.toHaveAttribute('aria-current')
  })

  it('offers "My appointments", which opens the patient’s appointments — and home itself asks for none of them', async () => {
    signIn()
    const mine = myAppointmentsEndpoints([anAppointment()])
    const api = serve({ [ME]: ok(me()), ...mine.routes })
    const { user, router } = renderApp('/')
    await screen.findByText('No hospital linked yet')
    expect(screen.getByRole('heading', { level: 2, name: 'Your appointments' })).toBeInTheDocument()
    expect(api.sent.map((request) => request.url)).toEqual(['/me'])

    await user.click(screen.getByRole('link', { name: 'My appointments' }))

    expect(await screen.findByRole('heading', { level: 1, name: 'My appointments' })).toHaveFocus()
    expect(router.state.location.pathname).toBe('/appointments')
    expect(await screen.findByRole('heading', { level: 3, name: 'Asha Menon' })).toBeInTheDocument()
  })

  it('has "Appointments" in the navigation, current on the list and on one appointment’s page', async () => {
    signIn()
    const mine = myAppointmentsEndpoints([anAppointment()])
    serve({ [ME]: ok(me()), ...mine.routes })
    const { user, router } = renderApp('/')
    await screen.findByRole('heading', { name: 'Welcome' })

    const navigation = within(screen.getByRole('navigation', { name: 'Main' }))
    expect(navigation.getByRole('link', { name: 'Appointments' })).toHaveAttribute('href', '/appointments')
    expect(navigation.getByRole('link', { name: 'Appointments' })).not.toHaveAttribute('aria-current')

    await user.click(navigation.getByRole('link', { name: 'Appointments' }))
    expect(await screen.findByRole('heading', { level: 1, name: 'My appointments' })).toBeInTheDocument()
    expect(navigation.getByRole('link', { name: 'Appointments' })).toHaveAttribute('aria-current', 'page')
    expect(navigation.getByRole('link', { name: 'Home' })).not.toHaveAttribute('aria-current')

    await user.click(await screen.findByRole('link', { name: /^View appointment Asha Menon/ }))
    expect(await screen.findByRole('heading', { level: 1, name: 'Appointment' })).toHaveFocus()
    expect(router.state.location.pathname).toBe(`/appointments/${myAppointmentRef(1)}`)
    expect(navigation.getByRole('link', { name: 'Appointments' })).toHaveAttribute('aria-current', 'page')
  })

  it('opens a linked hospital’s page from its card, by the reference /me gives', async () => {
    signIn()
    const api = serve({ [ME]: ok(me([cityCare, lakeside])), 'GET /hospitals/city-care': ok(cityCareHospital) })
    const { user, router } = renderApp('/')

    const card = await screen.findByRole('link', { name: 'City Care Hospital' })
    expect(card).toHaveAttribute('href', '/hospitals/city-care')
    // A paused link still has a hospital page; the server decides what it shows.
    expect(screen.getByRole('link', { name: 'Lakeside Clinic' })).toHaveAttribute('href', '/hospitals/lakeside-clinic')

    await user.click(card)

    expect(await screen.findByRole('heading', { level: 1, name: 'City Care Hospital' })).toHaveFocus()
    expect(router.state.location.pathname).toBe('/hospitals/city-care')
    // The path is the public reference — the internal id from /me is never put in a URL.
    expect(api.sent.map((request) => request.url)).toEqual(['/me', '/hospitals/city-care'])
    expect(JSON.stringify(api.sent.map((request) => [request.url, request.params]))).not.toContain('hosp-1')
  })

  it('escapes the reference, so a link’s hospital cannot point the card anywhere else', async () => {
    signIn()
    serve({ [ME]: ok(me([{ ...cityCare, hospital_ref: '../../login?next=//evil.example' }])) })
    renderApp('/')

    expect(await screen.findByRole('link', { name: 'City Care Hospital' })).toHaveAttribute(
      'href',
      '/hospitals/..%2F..%2Flogin%3Fnext%3D%2F%2Fevil.example',
    )
  })

  it('shows a linked hospital as plain text when the server sends no reference for it', async () => {
    signIn()
    const { hospital_ref: _omitted, ...withoutRef } = cityCare
    serve({ [ME]: ok(me([withoutRef as HospitalLink, { ...lakeside, hospital_ref: '' }])) })
    renderApp('/')

    const list = await screen.findByRole('list', { name: 'Your hospitals' })
    expect(within(list).getByText('City Care Hospital')).toBeInTheDocument()
    expect(within(list).getByText('Lakeside Clinic')).toBeInTheDocument()
    expect(within(list).queryByRole('link')).not.toBeInTheDocument()
  })
})
