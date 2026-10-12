import { act, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { afterEach, describe, expect, it } from 'vitest'
import { availabilityRoute } from '@/test/availability'
import { doctorDirectory } from '@/test/doctorDirectory'
import { deferred, fail, headerOf, ok, serve, unreachable, type Outcome, type Routes } from '@/test/fakeApi'
import { ashaRao, cardiology, cityCareHospital, doctor, lakesideHospital, meeraIyer, orthopaedics, vikramShah } from '@/test/fixtures'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

const HOSPITAL = 'GET /hospitals/city-care'
const LIST = `${HOSPITAL}/doctors`
const ASHA = `${LIST}/${ashaRao.ref}`
const VIKRAM = `${LIST}/${vikramShah.ref}`
const REFRESH = 'POST /auth/refresh'
const DOCTORS_PATH = '/hospitals/city-care/doctors'
const ASHA_PATH = `${DOCTORS_PATH}/${ashaRao.ref}`
const VIKRAM_PATH = `${DOCTORS_PATH}/${vikramShah.ref}`

const THREE = [ashaRao, meeraIyer, vikramShah]

const cityCareWith = (doctors = THREE): Routes => ({
  [HOSPITAL]: ok(cityCareHospital),
  ...doctorDirectory('city-care', doctors, [cardiology, orthopaedics]),
})

function open(path = ASHA_PATH) {
  signIn('access-1')
  return renderApp(path)
}

const main = () => screen.getByRole('main')
const title = (name: string) => screen.findByRole('heading', { level: 1, name })
const routeOf = (request: { method?: string; url?: string }) => `${request.method?.toUpperCase()} ${request.url}`
const notFound = () => fail(404, 'RESOURCE_NOT_FOUND', 'server wording that must not be shown')
/** The value beside a label in the details list. */
const detail = (label: string) => screen.getByText(label, { selector: 'dt' }).nextElementSibling as HTMLElement

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i
/** Anything that would read as a time, a date, a slot or a way to book one. */
const SCHEDULE =
  /\d{1,2}[:.]\d{2}|\b\d{1,2}\s?(am|pm)\b|\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\b(today|tomorrow|tonight|morning|afternoon|evening)\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d|\bslots?\b|next available|book now|\bbooked\b|\bfull\b|waiting list|coming soon|\bsoon\b|next week|launch/i

describe('doctor discovery — one doctor', () => {
  it('shows a loading state while the doctor is fetched', async () => {
    const { handler, answer } = deferred()
    serve({ ...cityCareWith(), [ASHA]: handler })
    open()

    expect(await screen.findByRole('status', { name: 'Loading doctor…' })).toBeInTheDocument()
    expect(main().querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument()

    answer(ok(ashaRao))

    expect(await title('Asha Rao')).toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
  })

  it('shows everything the API says about a doctor, the hospital they belong to, and the way to availability', async () => {
    const api = serve(cityCareWith())
    open()
    await title('Asha Rao')

    expect(screen.getByText('Interventional Cardiology')).toBeInTheDocument()
    expect(detail('Department')).toHaveTextContent(/^Cardiology$/)
    expect(detail('Languages')).toHaveTextContent(/^English, Kannada$/)

    // Each qualification in full: the degree, then where and when — only as far as they are known.
    const qualifications = within(detail('Qualifications')).getAllByRole('listitem')
    expect(qualifications).toHaveLength(2)
    expect(qualifications[0]).toHaveTextContent(/^MBBSBangalore Medical College, 2008$/)
    expect(qualifications[1]).toHaveTextContent(/^MD$/)

    // The bio is one paragraph of text; its line break is kept by CSS, not by markup.
    const bio = screen.getByText(/Looks after adults with heart conditions\./)
    expect(screen.getByRole('heading', { level: 2, name: 'About' })).toBeInTheDocument()
    expect(bio.textContent).toBe('Looks after adults with heart conditions.\nSees patients at the main campus.')
    expect(bio).toHaveClass('whitespace-pre-line', 'break-words')
    expect(bio.children).toHaveLength(0)

    // The hospital is named and linked; the list is one step back.
    expect(within(detail('Hospital')).getByRole('link', { name: 'City Care Hospital' })).toHaveAttribute('href', '/hospitals/city-care')
    expect(screen.getByRole('link', { name: 'Doctors at City Care Hospital' })).toHaveAttribute('href', DOCTORS_PATH)
    expect(screen.getByRole('link', { name: 'View Availability' })).toHaveAttribute('href', `${ASHA_PATH}/availability`)
    expect(document.title).toBe('Asha Rao · Atheris Health')

    // The name is as the hospital keeps it; nothing is put in front of it.
    expect(main()).not.toHaveTextContent(/\bDr\b/)
    // Two reads: the hospital, then the doctor — by public references, with the token and no parameters.
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA])
    expect(api.calls(ASHA)[0].params).toBeUndefined()
    expect(headerOf(api.calls(ASHA)[0], 'Authorization')).toBe('Bearer access-1')
  })

  it('leaves out, cleanly, every section a doctor has nothing for', async () => {
    serve(cityCareWith())
    open(VIKRAM_PATH)
    await title('Vikram Shah')

    expect(screen.getByText('General Medicine')).toBeInTheDocument()
    for (const label of ['Department', 'Qualifications', 'Languages']) {
      expect(screen.queryByText(label)).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('heading', { name: 'About' })).not.toBeInTheDocument()
    expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/null|undefined|N\/A|not (provided|specified|available)|—/i)
    // The hospital and the way on are still there.
    expect(detail('Hospital')).toHaveTextContent(/^City Care Hospital$/)
    expect(screen.getByRole('link', { name: 'View Availability' })).toBeInTheDocument()
    // Nothing but the doctor's own name has a number in it: there is none here at all.
    expect(main()).not.toHaveTextContent(/\d/)
  })

  it.each([
    ['empty strings and empty lists', { specialization: '', department: null, qualifications: [], languages: [], bio: '   ' }],
    ['missing fields', { specialization: undefined, department: undefined, qualifications: undefined, languages: undefined, bio: undefined }],
    ['the wrong kinds of thing', { specialization: 4, department: 'Cardiology', qualifications: 'MBBS', languages: [null, 7, ''], bio: ['x'] }],
  ])('a doctor with %s for the optional parts is a short page, not a broken one', async (_case, parts) => {
    serve({ ...cityCareWith(), [ASHA]: ok({ ref: ashaRao.ref, name: 'Asha Rao', ...parts }) })
    open()
    await title('Asha Rao')

    expect([...main().querySelectorAll('dt')].map((each) => each.textContent)).toEqual(['Hospital'])
    expect(screen.queryByRole('heading', { level: 2 })).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/null|undefined|\[object/)
    expect(screen.getByRole('link', { name: 'View Availability' })).toBeInTheDocument()
  })

  it('NEVER SHOWS A REFERENCE, and nothing the contract does not carry', async () => {
    const leaky = {
      ...ashaRao,
      id: '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55',
      user_id: '7a7a7a7a-1111-4222-8333-444444444444',
      license_number: 'KMC-SECRET-77',
      email: 'asha@secret.example',
      phone: '+91 99999 00000',
      rating: 4.8,
      reviews: [{ text: 'SECRET-REVIEW' }],
      consultation_fee: '750.00',
      experience_years: 17,
      slots: [{ start: '2026-10-08T09:30:00Z' }],
      next_available: '2026-10-08T09:30:00Z',
      photo_url: 'https://cdn.example.test/doctors/asha.png',
      sponsored: true,
      listing: 'promoted',
    }
    serve({ ...cityCareWith(), [ASHA]: ok(leaky) })
    open()
    await title('Asha Rao')

    expect(document.body.textContent).not.toMatch(UUID)
    for (const element of document.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        if (attribute.name !== 'href') expect(attribute.value).not.toMatch(UUID)
      }
    }
    const page = document.body.innerHTML
    for (const secret of ['5f0c2a9e', '7a7a7a7a', 'KMC-SECRET', 'secret.example', '99999', 'SECRET-REVIEW', '750.00', '2026-10-08', '09:30', 'cdn.example.test']) {
      expect(page).not.toContain(secret)
    }
    expect(main()).not.toHaveTextContent(/rating|review|\bfee\b|₹|experience|\bslot|sponsored|promoted|featured|email|phone|licen[cs]e/i)
    expect(document.querySelector('main img, main [src], main [style]')).toBeNull()
    // The doctor's own data is all the numbers there are: the year of a degree.
    expect(main().textContent?.match(/\d+/g)).toEqual(['2008'])
  })

  it('ATTACK — markup in any field of a doctor is shown as the characters it is', async () => {
    const hostile = doctor({
      name: '<img src=x onerror="window.pwned=1">Asha',
      specialization: '<script>window.pwned=1</script>Cardiology',
      department: { ref: cardiology.ref, name: '<b>Heart</b>' },
      qualifications: [{ degree: '<i>MBBS</i>', institution: '<u>College</u><iframe src="javascript:window.pwned=1"></iframe>', year: 2008 }],
      languages: ['<svg onload="window.pwned=1">English'],
      bio: '<a href="javascript:window.pwned=1">Click</a>\n<style>body{display:none}</style>Second line',
    })
    serve({ ...cityCareWith(), [ASHA]: ok(hostile) })
    const { user } = open()
    await title('<img src=x onerror="window.pwned=1">Asha')

    for (const literal of [
      '<script>window.pwned=1</script>Cardiology',
      '<b>Heart</b>',
      '<i>MBBS</i>',
      '<u>College</u><iframe src="javascript:window.pwned=1"></iframe>, 2008',
      '<svg onload="window.pwned=1">English',
    ]) {
      expect(screen.getByText(literal)).toBeInTheDocument()
    }
    expect(screen.getByText(/Second line/).textContent).toBe(hostile.bio)
    expect(main().querySelector('script, iframe, img, b, i, u, style, svg[onload], [onerror], [onclick], a[href^="javascript"]')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
    expect(document.title).toBe('<img src=x onerror="window.pwned=1">Asha · Atheris Health')

    // The same on the availability page, which names the doctor too.
    await user.click(screen.getByRole('link', { name: 'View Availability' }))
    await title('Availability')
    expect(screen.getByText('<img src=x onerror="window.pwned=1">Asha')).toBeInTheDocument()
    expect(main().querySelector('script, iframe, img, b, i, u, style, [onerror]')).toBeNull()
  })

  describe('not found', () => {
    it.each([
      ['an unknown or hidden doctor', ASHA_PATH],
      ['the availability of one', `${ASHA_PATH}/availability`],
    ])('NOT FOUND — %s: one neutral page, with the way back to the hospital’s doctors', async (_case, path) => {
      const api = serve({ ...cityCareWith(), [ASHA]: notFound() })
      open(path)

      const heading = await title('This doctor is not available')
      expect(heading).toBeInTheDocument()
      expect(screen.getByText('They may not be listed in the app, or the link you followed may be wrong.')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Browse doctors' })).toHaveAttribute('href', DOCTORS_PATH)
      expect(document.body).not.toHaveTextContent(/server wording|Asha|Cardiology/)
      expect(screen.queryByRole('link', { name: 'View Availability' })).not.toBeInTheDocument()
      // A 404 is the server's answer: it is not asked again.
      expect(api.calls(ASHA)).toHaveLength(1)
    })

    it('NOT FOUND — a doctor of another hospital is the same page: the answer is the server’s, for this hospital', async () => {
      // Asha Rao is listed at City Care; Lakeside Clinic is asked for her and says it has no such doctor.
      const api = serve({
        'GET /hospitals/lakeside-clinic': ok(lakesideHospital),
        [`GET /hospitals/lakeside-clinic/doctors/${ashaRao.ref}`]: notFound(),
      })
      open(`/hospitals/lakeside-clinic/doctors/${ashaRao.ref}`)

      expect(await title('This doctor is not available')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Browse doctors' })).toHaveAttribute('href', '/hospitals/lakeside-clinic/doctors')
      expect(api.sent.map(routeOf)).toEqual(['GET /hospitals/lakeside-clinic', `GET /hospitals/lakeside-clinic/doctors/${ashaRao.ref}`])
    })

    it('NOT FOUND — an unknown hospital is the hospital’s own page, and its doctor is never asked for', async () => {
      const api = serve({ [HOSPITAL]: notFound() })
      open()

      expect(await title('This hospital is not available')).toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Browse hospitals' })).toHaveAttribute('href', '/hospitals')
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL])
    })

    it.each([
      ['words', 'asha-rao'],
      ['a number', '42'],
      ['a path step', '..%2F..%2Fme'],
      ['a dot', '%2E%2E'],
      ['a reference with a query after it', `${ashaRao.ref}%3Fx=1`],
      ['a reference with something after it', `${ashaRao.ref}0`],
      ['a reference without its hyphens', ashaRao.ref.replaceAll('-', '')],
    ])('ATTACK — a doctor reference that is %s is never sent, on the profile or the availability page', async (_case, ref) => {
      const api = serve({ [HOSPITAL]: ok(cityCareHospital) })
      const { router } = open(`${DOCTORS_PATH}/${ref}`)

      expect(await title('This doctor is not available')).toBeInTheDocument()
      // Only the hospital was read.
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL])

      await act(() => router.navigate(`${DOCTORS_PATH}/${ref}/availability`))

      expect(await title('This doctor is not available')).toBeInTheDocument()
      expect(api.sent.map(routeOf)).toEqual([HOSPITAL])
    })

    it.each([
      ['a path step', '..%2F..%2Fme'],
      ['a query in the segment', 'city-care%3Fx=1'],
      ['more than a reference holds', 'a'.repeat(101)],
    ])('ATTACK — a hospital reference that is %s sends nothing at all, for the hospital or the doctor', async (_case, ref) => {
      const api = serve({})
      open(`/hospitals/${ref}/doctors/${ashaRao.ref}`)

      expect(await title('This hospital is not available')).toBeInTheDocument()
      expect(api.sent).toHaveLength(0)
    })
  })

  describe('when it cannot load', () => {
    afterEach(() => onlineManager.setOnline(true))

    const GATEWAY_PAGE: Outcome = {
      status: 503,
      data: '<html><body><h1>503 Service Temporarily Unavailable</h1><script>window.pwned=1</script>nginx</body></html>',
    }

    it.each([
      ['the profile', ASHA_PATH, 'Asha Rao'],
      ['the availability page', `${ASHA_PATH}/availability`, 'Availability'],
    ])('on %s OFFLINE and 503 are told apart, in the app’s words, and Retry asks again', async (_case, path, heading) => {
      const api = serve({ ...cityCareWith(), [ASHA]: GATEWAY_PAGE })
      const { user } = open(path)

      const message = 'We could not load this doctor. Please try again.'
      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent(/No connection|internet connection/)
      expect(document.body).not.toHaveTextContent(/503|Service Temporarily|nginx|Network Error|Request failed/)
      expect((window as { pwned?: unknown }).pwned).toBeUndefined()
      // Not "not available": the doctor may well exist.
      expect(screen.queryByText('This doctor is not available')).not.toBeInTheDocument()
      expect(screen.getByRole('link', { name: 'Doctors at City Care Hospital' })).toHaveAttribute('href', DOCTORS_PATH)

      api.on({ [ASHA]: unreachable })
      await user.click(screen.getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No connection'))
      expect(screen.queryByText(message)).not.toBeInTheDocument()
      expect(api.calls(ASHA)).toHaveLength(2)

      api.on({ [ASHA]: ok(ashaRao) })
      await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await title(heading)).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api.calls(ASHA)).toHaveLength(3)
    })

    it('OFFLINE — a request held back for lack of a network says so, and resumes by itself', async () => {
      serve(cityCareWith())
      const { router } = open('/hospitals/city-care')
      await title('City Care Hospital')

      onlineManager.setOnline(false)
      await act(() => router.navigate(ASHA_PATH))

      expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
      expect(screen.queryByRole('status', { name: 'Loading doctor…' })).not.toBeInTheDocument()

      act(() => onlineManager.setOnline(true))

      expect(await title('Asha Rao')).toBeInTheDocument()
    })

    it.each([
      ['that is not a doctor at all', 'Asha Rao'],
      ['with no body', null],
      ['with no reference', { ...ashaRao, ref: undefined }],
      ['with a reference that is not one', { ...ashaRao, ref: '../../me' }],
      ['with no name', { ...ashaRao, name: '' }],
      ['that is a list', [ashaRao]],
    ])('a 200 %s is a failure with a retry, not a broken or half-empty profile', async (_case, body) => {
      const api = serve({ ...cityCareWith(), [ASHA]: ok(body) })
      const { user } = open()

      expect(await screen.findByRole('alert')).toHaveTextContent('We could not load this doctor. Please try again.')
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'View Availability' })).not.toBeInTheDocument()

      api.on({ [ASHA]: ok(ashaRao) })
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await title('Asha Rao')).toBeInTheDocument()
    })
  })

  describe('session', () => {
    it('an expired token: one refresh, the doctor asked for again with the new token, the profile shown', async () => {
      signIn('access-old')
      const seen: string[] = []
      const api = serve({
        ...cityCareWith(),
        [ASHA]: (config) => {
          seen.push(String(headerOf(config, 'Authorization')))
          return headerOf(config, 'Authorization') === 'Bearer access-new' ? ok(ashaRao) : fail(401, 'AUTHENTICATION_REQUIRED', 'server wording')
        },
        [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }),
      })
      const { router } = renderApp('/hospitals/city-care')
      await title('City Care Hospital')
      await act(() => router.navigate(ASHA_PATH))

      expect(await title('Asha Rao')).toBeInTheDocument()
      expect(api.calls(REFRESH)).toHaveLength(1)
      expect(seen).toEqual(['Bearer access-old', 'Bearer access-new'])
      expect(router.state.location.pathname).toBe(ASHA_PATH)
      expect(document.body).not.toHaveTextContent('server wording')
      expect(isSignedIn()).toBe(true)
    })

    it.each([
      ['the profile', ASHA_PATH],
      ['the availability page', `${ASHA_PATH}/availability`],
    ])('ATTACK — a dead session opening %s: one refresh, one sign-out, nothing of the doctor shown', async (_case, path) => {
      signIn('access-old')
      const api = serve({
        [HOSPITAL]: ok(cityCareHospital),
        [ASHA]: fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'),
        [REFRESH]: fail(401, 'UNAUTHORIZED'),
      })
      const { router } = renderApp(path)

      expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/login')
      expect(isSignedIn()).toBe(false)
      expect(api.calls(REFRESH)).toHaveLength(1)
      expect(api.calls(ASHA)).toHaveLength(1)
      expect(document.body).not.toHaveTextContent(/City Care|Asha|server wording|could not load/)
    })
  })
})

describe('doctor discovery — availability, the next step', () => {
  it('NO FAKE SLOTS — the profile shows no time, date, day, slot or count, whatever the doctor’s answer carries', async () => {
    const { handler, answer } = deferred()
    serve({ ...cityCareWith(), [ASHA]: handler })
    open()

    // While loading: placeholders for the page, and nothing that could be taken for a slot.
    expect(await screen.findByRole('status', { name: 'Loading doctor…' })).toBeInTheDocument()
    for (const role of ['list', 'listitem', 'table', 'grid', 'button', 'radio', 'checkbox']) {
      expect(within(main()).queryByRole(role)).not.toBeInTheDocument()
    }
    expect(main()).not.toHaveTextContent(SCHEDULE)

    answer(
      ok({
        ...ashaRao,
        slots: [{ start: '2026-10-08T09:30:00Z', end: '2026-10-08T09:45:00Z', status: 'available' }],
        availability: { monday: ['09:30', '10:00'], next_available: 'Tomorrow 9:30 am' },
        available_today: true,
        slots_left: 4,
      }),
    )
    await title('Asha Rao')

    expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
    for (const role of ['table', 'grid', 'img', 'article', 'searchbox', 'combobox', 'button', 'radio', 'checkbox', 'textbox']) {
      expect(within(main()).queryByRole(role)).not.toBeInTheDocument()
    }
    expect(main().querySelector('input, select, textarea, time, form')).toBeNull()
    expect(main()).not.toHaveTextContent(SCHEDULE)
    expect(main().innerHTML).not.toMatch(/2026|09:30|10:00|slots_left|available_today/)
    // The year of a degree is the only number of the doctor's own.
    expect(main().textContent?.match(/\d+/g)).toEqual(['2008'])
  })

  it('the availability page shows what the availability endpoint returns, and nothing the doctor’s own answer carries', async () => {
    const leakyDoctor = {
      ...ashaRao,
      slots: [{ start: '2026-10-08T09:30:00Z', end: '2026-10-08T09:45:00Z', status: 'available' }],
      availability: { monday: ['09:30', '10:00'], next_available: 'Tomorrow 9:30 am' },
      available_today: true,
      slots_left: 4,
    }
    // The availability endpoint has one free slot in the whole week: Thursday at 14:00.
    const api = serve({
      ...cityCareWith(),
      [ASHA]: ok(leakyDoctor),
      ...availabilityRoute('city-care', ashaRao.ref, { slotsOn: (date) => (date === '2026-10-08' ? ['14:00'] : []) }),
    })
    const { user } = open()
    await title('Asha Rao')

    await user.click(screen.getByRole('link', { name: 'View Availability' }))

    expect(await title('Availability')).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /^14:00 to 14:15, Thursday 8 October 2026$/ })).toBeInTheDocument()
    expect(screen.getByText('Asha Rao')).toBeInTheDocument()
    expect(screen.getByText('City Care Hospital')).toBeInTheDocument()
    expect(document.title).toBe('Availability · Atheris Health')
    expect(screen.getByRole('link', { name: 'Back to Asha Rao' })).toHaveAttribute('href', ASHA_PATH)
    // Nothing of the doctor's own "availability" is on the page: the endpoint's answer is the only one.
    expect(main().innerHTML).not.toMatch(/09:30|10:00|Tomorrow|slots_left|available_today|next_available/)
    expect(main()).not.toHaveTextContent(/4 (free )?slots/)
    // The hospital, the doctor, and then — only once on the availability page — the availability, with no dates.
    expect(api.sent.map(routeOf)).toEqual([HOSPITAL, ASHA, `${ASHA}/availability`])
    expect(api.calls(`${ASHA}/availability`)[0].params).toBeUndefined()
  })
})

describe('doctor discovery — navigation', () => {
  it('goes from a hospital to its doctors, a profile and availability, and back up, with focus on each new heading', async () => {
    const api = serve({ ...hospitalDirectory([cityCareHospital]), ...doctorDirectory('city-care', THREE) })
    const { user, router } = open('/hospitals/city-care')
    await title('City Care Hospital')

    await user.click(screen.getByRole('link', { name: 'View Doctors' }))

    expect(await title('Doctors at City Care Hospital')).toHaveFocus()
    expect(router.state.location.pathname).toBe(DOCTORS_PATH)

    await user.click(await screen.findByRole('link', { name: 'View Profile Asha Rao' }))

    expect(await title('Asha Rao')).toHaveFocus()
    expect(router.state.location.pathname).toBe(ASHA_PATH)
    expect(router.state.location.search).toBe('')
    // Still under "Hospitals" in the navigation.
    expect(screen.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')

    await user.click(screen.getByRole('link', { name: 'View Availability' }))

    expect(await title('Availability')).toHaveFocus()
    expect(router.state.location.pathname).toBe(`${ASHA_PATH}/availability`)

    // A day, a time, and on to booking.
    await user.click(await screen.findByRole('button', { name: 'Friday 9 October 2026, 2 free slots' }))
    await user.click(screen.getByRole('button', { name: /^10:15 to 10:30, Friday 9 October 2026$/ }))
    await user.click(screen.getByRole('link', { name: 'Continue to booking' }))

    expect(await title('Booking')).toHaveFocus()
    expect(router.state.location.pathname).toBe(`${ASHA_PATH}/book`)
    expect(screen.getByText('Friday 9 October 2026')).toBeInTheDocument()
    expect(screen.getByText('10:15 – 10:30')).toBeInTheDocument()

    // Back to the availability as it was left: the same day and time chosen.
    await user.click(screen.getAllByRole('link', { name: 'Back to availability' })[0])
    expect(await title('Availability')).toHaveFocus()
    expect(router.state.location.search).toBe('?date=2026-10-09&slot=2026-10-09T10%3A15%3A00%2B05%3A30')
    expect(await screen.findByRole('button', { name: /^10:15 to 10:30/ })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('link', { name: 'Back to Asha Rao' }))
    expect(await title('Asha Rao')).toHaveFocus()

    await user.click(screen.getByRole('link', { name: 'Doctors at City Care Hospital' }))
    expect(await title('Doctors at City Care Hospital')).toHaveFocus()
    expect(router.state.location.pathname).toBe(DOCTORS_PATH)

    await user.click(screen.getByRole('link', { name: 'Back to City Care Hospital' }))
    expect(await title('City Care Hospital')).toHaveFocus()

    // Each thing was read once and reused on the way: the hospital, the list, the departments, the doctor, the availability.
    expect(api.sent.map(routeOf).sort()).toEqual([HOSPITAL, `${HOSPITAL}/departments`, LIST, ASHA, `${ASHA}/availability`].sort())
    // Nothing on the way booked, held or reserved anything.
    expect(api.sent.every((request) => (request.method ?? 'get').toLowerCase() === 'get')).toBe(true)
  })

  it('BACK from a profile returns to the list as it was: the search, the department and the page', async () => {
    serve(cityCareWith())
    const { user, router } = open(`${DOCTORS_PATH}?q=a&dept=${orthopaedics.ref}`)

    await user.click(await screen.findByRole('link', { name: 'View Profile Meera Iyer' }))
    await title('Meera Iyer')

    await act(() => router.navigate(-1))

    expect(await screen.findByRole('searchbox')).toHaveValue('a')
    expect(router.state.location.search).toBe(`?q=a&dept=${orthopaedics.ref}`)
    expect(await screen.findByRole('link', { name: 'View Profile Meera Iyer' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'View Profile Asha Rao' })).not.toBeInTheDocument()
  })

  it('moving from one doctor to another starts a fresh page: nothing of the first is left on the second', async () => {
    serve(cityCareWith())
    const { router } = open()
    await title('Asha Rao')

    await act(() => router.navigate(VIKRAM_PATH))

    expect(await title('Vikram Shah')).toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/Asha|Cardiology|MBBS|Kannada|Looks after/)
    expect(document.title).toBe('Vikram Shah · Atheris Health')
  })

  it('a profile link the list gave that the server no longer answers lands on the heading that says so', async () => {
    const api = serve({ ...cityCareWith(), [VIKRAM]: notFound() })
    const { user } = open(DOCTORS_PATH)

    await user.click(await screen.findByRole('link', { name: 'View Profile Vikram Shah' }))

    expect(await title('This doctor is not available')).toHaveFocus()
    expect(document.body).not.toHaveTextContent('server wording')
    expect(api.calls(VIKRAM)).toHaveLength(1)
  })

  it('a hospital opened by another reference still asks for and links its doctors by the public one', async () => {
    const INTERNAL_ID = '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55'
    const api = serve({ [`GET /hospitals/${INTERNAL_ID}`]: ok(cityCareHospital), ...doctorDirectory('city-care', [ashaRao]) })
    open(`/hospitals/${INTERNAL_ID}/doctors/${ashaRao.ref}`)
    await title('Asha Rao')

    expect(api.sent.map(routeOf)).toEqual([`GET /hospitals/${INTERNAL_ID}`, ASHA])
    expect(main().innerHTML).not.toContain('5f0c2a9e')
    expect(screen.getByRole('link', { name: 'View Availability' })).toHaveAttribute('href', `${ASHA_PATH}/availability`)
  })
})
