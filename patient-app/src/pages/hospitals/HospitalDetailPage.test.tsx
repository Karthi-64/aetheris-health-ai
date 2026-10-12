import { act, cleanup, fireEvent, screen, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { afterEach, describe, expect, it } from 'vitest'
import { bodyOf, deferred, fail, headerOf, ok, serve, unreachable } from '@/test/fakeApi'
import { cityCare, cityCareHospital, hospital, lakesideHospital, me, promotedHospital, sunriseHospital } from '@/test/fixtures'
import { doctorDirectory } from '@/test/doctorDirectory'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { renderApp, signIn } from '@/test/renderApp'

const CITY_CARE = 'GET /hospitals/city-care'
const LAKESIDE = 'GET /hospitals/lakeside-clinic'

function open(path = '/hospitals/city-care') {
  signIn('access-1')
  return renderApp(path)
}

const title = (name: string) => screen.findByRole('heading', { level: 1, name })
const notFound = (wording = 'server wording that must not be shown') => fail(404, 'RESOURCE_NOT_FOUND', wording)

describe('hospital discovery — one hospital', () => {
  it('shows a loading state while the hospital is fetched', async () => {
    const { handler, answer } = deferred()
    serve({ [CITY_CARE]: handler })
    open()

    expect(await screen.findByRole('status', { name: 'Loading hospital…' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()

    answer(ok(cityCareHospital))

    expect(await title('City Care Hospital')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows everything the patient needs to reach a linked hospital', async () => {
    const api = serve({ [CITY_CARE]: ok(cityCareHospital) })
    open()
    await title('City Care Hospital')

    // The logo is the address the server gave, loaded without telling its host where from.
    const logo = screen.getByRole('img', { name: 'City Care Hospital logo' })
    expect(logo).toHaveAttribute('src', 'https://cdn.example.test/logos/city-care.png')
    expect(logo).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(screen.getAllByRole('img')).toHaveLength(1)

    for (const line of ['12 MG Road', 'Indiranagar', 'Bengaluru, Karnataka 560038', 'India']) {
      expect(screen.getByText(line)).toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: '+91 80 5550 0100' })).toHaveAttribute('href', 'tel:+918055500100')
    expect(screen.getByText('Asia/Kolkata')).toBeInTheDocument()

    expect(screen.getByText('Linked')).toBeInTheDocument()
    expect(screen.getByText('Your record at this hospital is linked to your account.')).toBeInTheDocument()
    // Already linked: there is nothing to link.
    expect(screen.queryByRole('link', { name: 'Link my record' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View Doctors' })).toHaveAttribute('href', '/hospitals/city-care/doctors')

    const [request] = api.calls(CITY_CARE)
    expect(api.sent).toHaveLength(1)
    expect(request.params).toBeUndefined()
    expect(headerOf(request, 'Authorization')).toBe('Bearer access-1')
    expect(document.title).toBe('City Care Hospital · Atheris Health')
  })

  it('shows a hospital without a logo, a phone or a full address, and offers to link when not linked', async () => {
    serve({ [LAKESIDE]: ok(lakesideHospital) })
    open('/hospitals/lakeside-clinic')
    await title('Lakeside Clinic')

    // An initial-letter mark stands in for the logo; it is decoration, the name is beside it.
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText('L')).toHaveAttribute('aria-hidden', 'true')

    expect(screen.getByText('4 Lake View Road')).toBeInTheDocument()
    expect(screen.getByText('Mysuru, Karnataka')).toBeInTheDocument()
    expect(screen.queryByText('Phone')).not.toBeInTheDocument()
    expect(document.querySelector('a[href^="tel:"]')).toBeNull()
    expect(screen.getByRole('main')).not.toHaveTextContent(/null|undefined|,\s*$/)

    expect(screen.queryByText('Linked')).not.toBeInTheDocument()
    expect(screen.queryByText(/linked to your account/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Link my record' })).toHaveAttribute('href', '/link-patient')
    expect(screen.getByRole('link', { name: 'View Doctors' })).toHaveAttribute('href', '/hospitals/lakeside-clinic/doctors')
  })

  it('shows a phone that cannot be dialled as text, not as a link', async () => {
    serve({ [CITY_CARE]: ok(hospital({ phone: 'Ask at reception' })) })
    open()
    await title('City Care Hospital')

    expect(screen.getByText('Ask at reception')).toBeInTheDocument()
    expect(document.querySelector('a[href^="tel:"]')).toBeNull()
  })

  it('ATTACK — a phone number carrying a script is shown as the text it is, and is no link at all', async () => {
    serve({ [CITY_CARE]: ok(hospital({ phone: 'javascript:alert(1)//+91 80 5550 0100' })) })
    open()
    await title('City Care Hospital')

    expect(screen.getByText('javascript:alert(1)//+91 80 5550 0100')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /5550 0100/ })).not.toBeInTheDocument()
    expect(document.querySelector('a[href^="javascript"], a[href^="tel:"]')).toBeNull()
  })

  describe('logo', () => {
    it.each([
      ['a script', 'javascript:alert(document.cookie)'],
      ['an inline document', 'data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9ImFsZXJ0KDEpIi8+'],
      ['a path on this origin', '/api/v1/patient/auth/logout'],
      ['a relative path', 'logos/city-care.png'],
      ['a protocol-relative address', '//evil.example/logo.png'],
      ['an address without its slashes', 'https:evil.example/logo.png'],
      ['a capitalised scheme', 'HTTPS://cdn.example.test/logo.png'],
      ['leading whitespace', ' https://cdn.example.test/logo.png'],
      ['a line break inside', 'https://cdn.example.test/lo\ngo.png'],
      ['another scheme', 'ftp://cdn.example.test/logo.png'],
      ['a blob', 'blob:https://cdn.example.test/1234'],
      ['an empty string', ''],
      ['a number', 42],
      ['an object', { url: 'https://cdn.example.test/logo.png' }],
    ])('ATTACK — a logo address that is %s is never given to the browser', async (_case, logoUrl) => {
      serve({ [CITY_CARE]: ok({ ...cityCareHospital, logo_url: logoUrl }) })
      open()
      await title('City Care Hospital')

      // No image at all, and nothing of the address anywhere in the page.
      expect(document.querySelector('img')).toBeNull()
      expect(document.querySelector('[src], [srcset], [style*="url"]')).toBeNull()
      expect(screen.getByText('C')).toHaveAttribute('aria-hidden', 'true')
      if (typeof logoUrl === 'string' && logoUrl.trim() !== '') {
        expect(document.body.innerHTML).not.toContain(logoUrl.trim())
      }
    })

    it('falls back to the initial when the logo does not load', async () => {
      serve({ [CITY_CARE]: ok(cityCareHospital) })
      open()
      await title('City Care Hospital')

      fireEvent.error(screen.getByRole('img', { name: 'City Care Hospital logo' }))

      expect(screen.queryByRole('img')).not.toBeInTheDocument()
      expect(screen.getByText('C')).toHaveAttribute('aria-hidden', 'true')
    })
  })

  it('ATTACK — a response carrying more than the contract allows: nothing beyond the allow-list reaches the screen', async () => {
    const leaky = {
      ...hospital({ name: '<script>window.pwned=1</script>Evil & Sons' }),
      id: '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55',
      settings: { 'feature.patient_app.enabled': true, api_key: 'SETTINGS-SECRET' },
      tax_id: 'TAX-SECRET-99',
      email: 'admin@secret.example',
      is_active: true,
      updated_by: 'STAFF-SECRET',
      patient_count: 987654,
      distance_km: 4.2,
    }
    serve({ [CITY_CARE]: ok({ ...leaky, address: { ...leaky.address, gps: 'GPS-SECRET', notes: 'ADDRESS-SECRET' } }) })
    open()

    // Markup in a name is text.
    expect(await title('<script>window.pwned=1</script>Evil & Sons')).toBeInTheDocument()
    expect(document.querySelector('main script')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()

    const shown = document.body.textContent ?? ''
    for (const secret of ['5f0c2a9e', 'SETTINGS-SECRET', 'TAX-SECRET-99', 'secret.example', 'STAFF-SECRET', '987654', 'GPS-SECRET', 'ADDRESS-SECRET']) {
      expect(shown).not.toContain(secret)
    }
    expect(shown).not.toMatch(/4\.2|\bkm\b|distance|miles|away|near you/i)
  })

  it('SPONSORED — a promoted hospital says so on its own page, and a standard one never does', async () => {
    serve(hospitalDirectory([promotedHospital, sunriseHospital]))
    const { router } = open('/hospitals/harbour-health')
    await title('Harbour Health')

    expect(screen.getByText('Sponsored')).toBeInTheDocument()

    await act(() => router.navigate('/hospitals/sunrise-medical'))
    await title('Sunrise Medical Centre')

    expect(screen.queryByText(/sponsored|promoted|featured/i)).not.toBeInTheDocument()
  })

  describe('not found', () => {
    it('NOT FOUND — an unknown hospital and one that is switched off get the same neutral page', async () => {
      const pages: string[] = []
      for (const ref of ['no-such-hospital', 'switched-off']) {
        const api = serve({ [`GET /hospitals/${ref}`]: notFound(`server wording about ${ref}`) })
        open(`/hospitals/${ref}`)

        expect(await title('This hospital is not available')).toBeInTheDocument()
        expect(screen.getByText('It may not be available in the app, or the link you followed may be wrong.')).toBeInTheDocument()
        expect(screen.getByRole('link', { name: 'Browse hospitals' })).toHaveAttribute('href', '/hospitals')
        // The answer is final: asking again is not offered, and was not done.
        expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument()
        expect(api.sent).toHaveLength(1)
        // Nothing to act on for a hospital that is not there.
        expect(screen.queryByRole('link', { name: 'View Doctors' })).not.toBeInTheDocument()
        expect(screen.queryByRole('link', { name: 'Link my record' })).not.toBeInTheDocument()

        pages.push(screen.getByRole('main').textContent ?? '')
        cleanup()
      }

      expect(pages[0]).toBe(pages[1])
      expect(pages[0]).not.toMatch(/server wording|no-such-hospital|switched-off/)
    })

    it.each(['..%2F..%2Fme%3Fx', 'city-care%2Flink', 'city-care%3Fpage%3D2', 'city-care%23top', '%20city-care', 'a'.repeat(101)])(
      'ATTACK — a reference of "%s", which no hospital can have and a server could read as another path, is never sent',
      async (ref) => {
        const api = serve({})
        open(`/hospitals/${ref}`)

        expect(await title('This hospital is not available')).toBeInTheDocument()
        expect(api.sent).toHaveLength(0)
        expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
      },
    )

    it.each(['CITY-CARE', '0b6f1c1e-7c0a-4a52-9a43-0d4f4f2b9c11', 'a'.repeat(100)])(
      'a reference of "%s" is letters, digits and hyphens, so it is asked for',
      async (ref) => {
        const api = serve({ [`GET /hospitals/${ref}`]: notFound() })
        open(`/hospitals/${ref}`)

        expect(await title('This hospital is not available')).toBeInTheDocument()
        expect(api.sent.map((request) => request.url)).toEqual([`/hospitals/${ref}`])
      },
    )

    it.each(['..', '.'])(
      'ATTACK — a reference of "%s", which a browser would resolve to another endpoint, is never sent',
      async (ref) => {
        const api = serve({})
        open(`/hospitals/${ref}`)

        expect(await title('This hospital is not available')).toBeInTheDocument()
        expect(api.sent).toHaveLength(0)
      },
    )

    it('ATTACK — SQL and markup in a reference are never sent and get the same page', async () => {
      const ref = `x' OR '1'='1"><svg onload=alert(1)>`
      const path = `/hospitals/${encodeURIComponent(ref)}`
      const api = serve({})
      open(path)

      expect(await title('This hospital is not available')).toBeInTheDocument()
      expect(api.sent).toHaveLength(0)
      expect(document.querySelector('main svg[onload]')).toBeNull()
      expect(screen.getByRole('main')).not.toHaveTextContent("OR '1'='1")
    })
  })

  describe('when it cannot load', () => {
    afterEach(() => onlineManager.setOnline(true))

    it('ERROR — shows the app’s own message and a retry that works', async () => {
      const api = serve({ [CITY_CARE]: fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)') })
      const { user } = open()

      expect(await screen.findByRole('alert')).toHaveTextContent('We could not load this hospital. Please try again.')
      expect(document.body).not.toHaveTextContent('Traceback')
      // Not mistaken for a hospital that does not exist.
      expect(screen.queryByText('This hospital is not available')).not.toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'All hospitals' })).toHaveAttribute('href', '/hospitals')

      api.on({ [CITY_CARE]: ok(cityCareHospital) })
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await title('City Care Hospital')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it.each([
      ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot show this in the app right now\. Please try again later\.$/],
      ['the rate limit (429)', fail(429, 'RATE_LIMITED', 'server wording'), /^Too many requests\. Please wait a moment and try again\.$/],
      ['an unknown refusal (403)', fail(403, 'FORBIDDEN', 'server wording'), /^We could not load this hospital\. Please try again\.$/],
    ])('shows a safe message for %s', async (_case, outcome, message) => {
      serve({ [CITY_CARE]: outcome })
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent('server wording')
    })

    it('OFFLINE — a request that gets no answer is called a connection problem, and can be retried', async () => {
      const api = serve({ [CITY_CARE]: unreachable })
      const { user } = open()

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('No connection')
      expect(alert).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
      expect(screen.queryByText('This hospital is not available')).not.toBeInTheDocument()

      api.on({ [CITY_CARE]: ok(cityCareHospital) })
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await title('City Care Hospital')).toBeInTheDocument()
    })

    it('OFFLINE — a request held back for lack of a network says so, and resumes by itself', async () => {
      onlineManager.setOnline(false)
      const api = serve({ [CITY_CARE]: ok(cityCareHospital) })
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
      expect(screen.queryByRole('status', { name: 'Loading hospital…' })).not.toBeInTheDocument()
      expect(api.sent).toHaveLength(0)

      act(() => onlineManager.setOnline(true))

      expect(await title('City Care Hospital')).toBeInTheDocument()
    })
  })

  describe('navigation', () => {
    it('goes from the list to a hospital, on to its doctors page and back, with focus on each new heading', async () => {
      const api = serve({ ...hospitalDirectory([cityCareHospital, lakesideHospital]), ...doctorDirectory('city-care', []) })
      const { user, router } = open('/hospitals')

      await user.click(await screen.findByRole('link', { name: 'City Care Hospital' }))

      const hospitalHeading = await title('City Care Hospital')
      expect(router.state.location.pathname).toBe('/hospitals/city-care')
      expect(hospitalHeading).toHaveFocus()
      // Still under "Hospitals" in the navigation.
      expect(screen.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')

      await user.click(screen.getByRole('link', { name: 'View Doctors' }))

      const doctorsHeading = await title('Doctors at City Care Hospital')
      expect(router.state.location.pathname).toBe('/hospitals/city-care/doctors')
      expect(doctorsHeading).toHaveFocus()
      expect(screen.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')

      await user.click(screen.getByRole('link', { name: 'Back to City Care Hospital' }))

      expect(await title('City Care Hospital')).toHaveFocus()
      expect(router.state.location.pathname).toBe('/hospitals/city-care')

      await user.click(screen.getByRole('link', { name: 'All hospitals' }))

      expect(await title('Find a hospital')).toHaveFocus()
      expect(router.state.location.pathname).toBe('/hospitals')
      // The hospital was read once and reused by the doctors page and the way back.
      expect(api.calls(CITY_CARE)).toHaveLength(1)
    })

    it('does not move focus on first load: the browser starts at the top by itself', async () => {
      serve({ [CITY_CARE]: ok(cityCareHospital) })
      open()

      expect(await title('City Care Hospital')).not.toHaveFocus()
      expect(document.body).toHaveFocus()
    })

    it('hands the hospital’s code to the link form, which still accepts another', async () => {
      const linked = { hospital_id: 'hosp-2', hospital_ref: 'lakeside-clinic', hospital_name: 'Lakeside Clinic', linked_at: '2026-10-07T08:00:00Z', suspended: false }
      const api = serve({
        [LAKESIDE]: ok(lakesideHospital),
        'POST /hospitals/lakeside-clinic/link': ok(linked, 201),
        'GET /me': ok(me([cityCare])),
      })
      const { user, router } = open('/hospitals/lakeside-clinic')

      await user.click(await screen.findByRole('link', { name: 'Link my record' }))

      const code = await screen.findByLabelText('Hospital code')
      expect(router.state.location.pathname).toBe('/link-patient')
      expect(code).toHaveValue('lakeside-clinic')
      // The code is all that travels, and not in the URL.
      expect(router.state.location.search).toBe('')
      expect(router.state.location.state).toEqual({ hospitalCode: 'lakeside-clinic' })

      // It is a starting value: the patient can change it, and change it back.
      await user.clear(code)
      expect(code).toHaveValue('')
      await user.type(code, 'lakeside-clinic')
      await user.type(screen.getByLabelText('Date of birth'), '1990-05-17')
      await user.click(screen.getByRole('checkbox'))
      await user.click(screen.getByRole('button', { name: 'Find my record' }))

      expect(await screen.findByRole('heading', { name: 'Your record is linked' })).toBeInTheDocument()
      expect(bodyOf(api.calls('POST /hospitals/lakeside-clinic/link')[0])).toEqual({
        date_of_birth: '1990-05-17',
        consent_policy_version: '2026-10-draft',
      })

      // Back on the hospital's page the link shows without a reload: it is read again.
      api.on({ [LAKESIDE]: ok({ ...lakesideHospital, linked: true }) })
      await act(() => router.navigate('/hospitals/lakeside-clinic'))

      expect(await screen.findByText('Linked')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'Link my record' })).not.toBeInTheDocument()
      expect(api.calls(LAKESIDE)).toHaveLength(2)
    })

    it('opened on its own, the link form starts empty as before', async () => {
      serve({})
      open('/link-patient')

      expect(await screen.findByLabelText('Hospital code')).toHaveValue('')
    })

    it('ATTACK — router state that is not a code is ignored by the link form', async () => {
      serve({ 'GET /me': ok(me()) })
      const { router } = open('/')
      await screen.findByRole('heading', { name: 'Welcome' })

      for (const state of [{ hospitalCode: 42 }, { hospitalCode: 'x'.repeat(101) }, { hospitalCode: { toString: () => 'city-care' } }, 'city-care', null]) {
        await act(() => router.navigate('/link-patient', { state }))
        expect(await screen.findByLabelText('Hospital code')).toHaveValue('')
        await act(() => router.navigate('/'))
        await screen.findByRole('heading', { name: 'Welcome' })
      }
    })

    it('RESPONSIVE — touch-sized actions in one column, and a long name that wraps', async () => {
      serve({ [LAKESIDE]: ok({ ...lakesideHospital, name: 'Superspeciality'.repeat(8), phone: '+91 821 555 0100' }) })
      open('/hospitals/lakeside-clinic')
      const heading = await title('Superspeciality'.repeat(8))

      expect(heading).toHaveClass('break-words')
      const main = screen.getByRole('main')
      const controls = [...main.querySelectorAll('a, button')]
      // Back, call, View Doctors, Link my record.
      expect(controls).toHaveLength(4)
      for (const control of controls) expect(control.className).toMatch(/(^|\s)(h-11|min-h-11)(\s|$)/)
      expect(within(main).queryByRole('table')).not.toBeInTheDocument()
    })
  })
})
