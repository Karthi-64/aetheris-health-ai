import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { PatientHospital } from '@/api/hospitals'
import { queryClient } from '@/lib/query-client'
import { SEARCH_DEBOUNCE_MS } from '@/pages/hospitals/HospitalsPage'
import { fail, headerOf, ok, okPage, serve, unreachable, type Handler, type Outcome } from '@/test/fakeApi'
import { doctorDirectory } from '@/test/doctorDirectory'
import {
  ashaRao,
  cityCare,
  cityCareHospital,
  hospital,
  lakeside,
  lakesideHospital,
  me,
  promotedHospital,
  sunriseHospital,
  vikramShah,
} from '@/test/fixtures'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

/**
 * The rules of hospital discovery that a page can break while still rendering
 * correctly for well-formed data. Each test here names what must never reach
 * the screen (or the network) and feeds the app the response, the address or
 * the timing that would put it there.
 */

const LIST = 'GET /hospitals'
const CITIES = 'GET /hospital-cities'
const CITY_CARE = 'GET /hospitals/city-care'
const REFRESH = 'POST /auth/refresh'
const INTERNAL_ID = '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55'

const THREE = [cityCareHospital, lakesideHospital, sunriseHospital]

function open(path = '/hospitals') {
  signIn('access-1')
  return renderApp(path)
}

const main = () => screen.getByRole('main')
const title = (name: string) => screen.findByRole('heading', { level: 1, name })
const card = (name: string | RegExp) => screen.findByRole('link', { name })
const searchField = () => screen.findByRole('searchbox', { name: 'Search by hospital name' })
const position = () => screen.getByRole('status')
const routeOf = (request: { method?: string; url?: string }) => `${request.method?.toUpperCase()} ${request.url}`

const typed = (field: HTMLElement, value: string) => fireEvent.change(field, { target: { value } })
const pass = (ms: number) => act(() => vi.advanceTimersByTimeAsync(ms))

async function until(assertion: () => void) {
  for (let turn = 0; turn < 50; turn++) {
    try {
      return assertion()
    } catch {
      await pass(0)
    }
  }
  assertion()
}

/** Anything that would read as a doctor, a count of them, a specialty or a way to book one. */
const DOCTOR_DATA =
  /\bDr\.?\s|\bMBBS\b|\bMD\b|speciali[sz]|cardiolog|\d+\s+doctors?\b|years? of experience|available today|book now|consultation fee/i

/** The same, for a page that is about doctors and so may say "specialisation" in its own labels. */
const DOCTOR_RECORD =
  /\bDr\.?\s|\bMBBS\b|\bMD\b|cardiolog|\d+\s+doctors?\b|years? of experience|available today|book now|consultation fee/i

/** A hospital response that has grown doctor data its contract does not carry. */
const withDoctors = (base: PatientHospital) => ({
  ...base,
  doctor_count: 12,
  doctors: [{ id: 'doc-1', name: 'Dr. Asha Rao', specialty: 'Cardiology', qualification: 'MBBS, MD', fee: '500.00' }],
  specialties: ['Cardiology', 'Orthopaedics'],
  departments: [{ name: 'Cardiology', doctor_count: 4 }],
})

describe('NO INVENTED DOCTOR DATA — doctors come from the doctor endpoints and from nowhere else', () => {
  it('the hospital page shows no doctor, count, specialty or placeholder card — loading or loaded, whatever the response carries', async () => {
    let answer: (outcome: Outcome) => void = () => {}
    const pending = new Promise<Outcome>((resolve) => (answer = resolve))
    const api = serve({ [CITY_CARE]: () => pending })
    open('/hospitals/city-care')

    // Loading: the two placeholders are the page's own header, not cards.
    expect(await screen.findByRole('status', { name: 'Loading hospital…' })).toBeInTheDocument()
    expect(main().querySelectorAll('[data-slot="skeleton"]')).toHaveLength(2)
    for (const role of ['list', 'listitem', 'article', 'table']) {
      expect(within(main()).queryByRole(role)).not.toBeInTheDocument()
    }
    expect(main()).not.toHaveTextContent(/doctor/i)

    answer(ok(withDoctors(cityCareHospital)))
    await title('City Care Hospital')

    expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
    for (const role of ['list', 'listitem', 'article', 'table']) {
      expect(within(main()).queryByRole(role)).not.toBeInTheDocument()
    }
    expect(main()).not.toHaveTextContent(DOCTOR_DATA)
    expect(main().innerHTML).not.toMatch(/Asha|Rao|Cardiology|Orthopaedics|doc-1|500\.00/)
    // The word appears once: on the way to the doctors page.
    expect(main().textContent?.match(/doctor/gi)).toEqual(['Doctor'])
    expect(screen.getByRole('link', { name: 'View Doctors' })).toBeInTheDocument()
    expect(api.sent.map(routeOf)).toEqual([CITY_CARE])
  })

  it('the list shows none of it either, and the doctors page shows only what the doctor endpoints return — here, nobody', async () => {
    const api = serve({
      [LIST]: okPage([withDoctors(cityCareHospital)]),
      [CITIES]: ok({ cities: ['Bengaluru'] }),
      [CITY_CARE]: ok(withDoctors(cityCareHospital)),
      ...doctorDirectory('city-care', []),
    })
    const { user } = open()

    const listed = await card('City Care Hospital')
    expect(main()).not.toHaveTextContent(DOCTOR_DATA)
    expect(main()).not.toHaveTextContent(/doctor/i)
    expect(main().innerHTML).not.toMatch(/Asha|Rao|Cardiology|Orthopaedics|doc-1|500\.00/)

    await user.click(listed)
    await user.click(await screen.findByRole('link', { name: 'View Doctors' }))
    await title('Doctors at City Care Hospital')
    expect(await screen.findByText('No doctors listed yet')).toBeInTheDocument()

    // The hospital's answer carried a doctor; the doctor endpoints listed none, so none is shown.
    expect(main()).not.toHaveTextContent(DOCTOR_RECORD)
    expect(main().innerHTML).not.toMatch(/Asha|Rao|Cardiology|Orthopaedics|doc-1|500\.00/)
    // Not even the count: the page has no number on it at all.
    expect(main()).not.toHaveTextContent(/\d/)
    expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
    expect(within(main()).queryByRole('list')).not.toBeInTheDocument()
    expect(within(main()).queryByRole('link', { name: /View Profile/ })).not.toBeInTheDocument()
    // List, cities, the hospital, then its doctors and departments: the whole journey asks for nothing else.
    expect(api.sent.slice(0, 3).map(routeOf)).toEqual([LIST, CITIES, CITY_CARE])
    expect(api.sent.slice(3).map(routeOf).sort()).toEqual([`${CITY_CARE}/departments`, `${CITY_CARE}/doctors`])
  })
})

describe('SPONSORED — the label is shown if and only if listing is exactly "promoted"', () => {
  /** What a careless check (`!== 'standard'`, a truthy test, a loose match) would label. */
  const NOT_PROMOTED = ['PROMOTED', 'Promoted', ' promoted', 'promoted ', 'sponsored', 'featured', 'premium', '', true, 1, null, undefined, ['promoted'], { promoted: true }]

  it('ATTACK — a listing value that is not "promoted" never labels a card', async () => {
    const odd = NOT_PROMOTED.map((listing, index) => ({ ...hospital({ ref: `h-${index}`, name: `Hospital ${index}` }), listing }))
    serve({ [LIST]: okPage(odd), [CITIES]: ok({ cities: [] }) })
    open()

    await card('Hospital 0')
    expect(within(screen.getByRole('list', { name: 'Hospitals' })).getAllByRole('link')).toHaveLength(NOT_PROMOTED.length)
    expect(document.body).not.toHaveTextContent(/sponsored|promoted|featured|premium/i)
  })

  it.each(NOT_PROMOTED.map((listing) => [JSON.stringify(listing) ?? 'undefined', listing]))(
    'ATTACK — a hospital page whose listing is %s is not labelled',
    async (_case, listing) => {
      serve({ [CITY_CARE]: ok({ ...cityCareHospital, listing }) })
      open('/hospitals/city-care')
      await title('City Care Hospital')

      expect(document.body).not.toHaveTextContent(/sponsored|promoted|featured|premium/i)
      // The page is otherwise whole: the other mark is still there.
      expect(screen.getByText('Linked')).toBeInTheDocument()
    },
  )

  it('a promoted hospital the patient is linked to carries both marks, on its card and on its page', async () => {
    serve(hospitalDirectory([{ ...promotedHospital, linked: true }, lakesideHospital]))
    const { user } = open()

    const promoted = await card('Harbour Health')
    expect(promoted).toHaveAccessibleDescription('Kochi, Kerala Sponsored Linked')
    expect(screen.getAllByText('Sponsored')).toHaveLength(1)
    expect(screen.getByRole('link', { name: 'Lakeside Clinic' })).toHaveAccessibleDescription('Mysuru, Karnataka')

    await user.click(promoted)
    await title('Harbour Health')

    // Visible text, not a tooltip or a hidden hint.
    const label = screen.getByText('Sponsored')
    expect(label).toBeVisible()
    expect(label.closest('[aria-hidden="true"], [hidden]')).toBeNull()
    expect(screen.getByText('Linked')).toBeInTheDocument()
  })
})

describe('NO DISTANCE, NO INTERNAL ID — neither is shown, kept in the page, or put in an address', () => {
  const leaky = {
    ...lakesideHospital,
    id: INTERNAL_ID,
    hospital_id: INTERNAL_ID,
    distance: 4.2,
    distance_km: 4.2,
    distance_text: '4.2 km away',
    latitude: 12.9716,
    longitude: 77.5946,
  }
  const NEVER = /5f0c2a9e|4\.2|12\.9716|77\.5946|\bkm\b|kilomet|distance|miles?\b|\baway\b|near(by| you| me)|latitude|longitude/i

  it('ATTACK — a response carrying an id and a distance: nothing of them in the text, the attributes or the links, on any discovery screen', async () => {
    serve({ [LIST]: okPage([leaky]), [CITIES]: ok({ cities: ['Mysuru'] }), 'GET /hospitals/lakeside-clinic': ok(leaky), ...doctorDirectory('lakeside-clinic', [ashaRao]) })
    const { user, router } = open()

    const listed = await card('Lakeside Clinic')
    // `innerHTML`, not text: a `data-id`, a `key` leaked into an id, or a title would show here.
    expect(document.body.innerHTML).not.toMatch(NEVER)
    expect(listed).toHaveAttribute('href', '/hospitals/lakeside-clinic')

    await user.click(listed)
    await title('Lakeside Clinic')
    expect(document.body.innerHTML).not.toMatch(NEVER)

    await user.click(screen.getByRole('link', { name: 'View Doctors' }))
    await title('Doctors at Lakeside Clinic')
    await screen.findByRole('heading', { level: 3, name: 'Asha Rao' })
    expect(document.body.innerHTML).not.toMatch(NEVER)
    expect(router.state.location.pathname).toBe('/hospitals/lakeside-clinic/doctors')
  })

  it('a hospital opened by its internal id shows and links only its public reference', async () => {
    const api = serve({ [`GET /hospitals/${INTERNAL_ID}`]: ok(lakesideHospital) })
    const { user, router } = open(`/hospitals/${INTERNAL_ID}`)
    await title('Lakeside Clinic')

    expect(main().innerHTML).not.toContain('5f0c2a9e')
    expect(screen.getByRole('link', { name: 'View Doctors' })).toHaveAttribute('href', '/hospitals/lakeside-clinic/doctors')

    await user.click(screen.getByRole('link', { name: 'Link my record' }))

    // The code handed to the link form is the reference the server gave, not what was in the address bar.
    expect(await screen.findByLabelText('Hospital code')).toHaveValue('lakeside-clinic')
    expect(router.state.location.state).toEqual({ hospitalCode: 'lakeside-clinic' })
    expect(api.sent).toHaveLength(1)
  })

  it('home shows linked hospitals without putting the id /me gives into the page', async () => {
    serve({ 'GET /me': ok(me([cityCare, lakeside])) })
    open('/')

    await card('City Care Hospital')
    expect(document.body.innerHTML).not.toMatch(/hosp-1|hosp-2/)
    expect(document.body.innerHTML).not.toMatch(NEVER)
  })
})

describe('LOGO — an address the app will not load is never in the page', () => {
  afterEach(() => vi.unstubAllEnvs())

  const HTTP_LOGO = 'http://cdn.example.test/logos/city-care.png'

  it('ATTACK — in a production build an http:// logo is refused: no image, and the address is nowhere in the page', async () => {
    vi.stubEnv('DEV', false)
    serve({ [CITY_CARE]: ok({ ...cityCareHospital, logo_url: HTTP_LOGO }) })
    open('/hospitals/city-care')
    await title('City Care Hospital')

    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('[src], [srcset], [style*="url"]')).toBeNull()
    expect(document.body.innerHTML).not.toContain('cdn.example.test')
    expect(screen.getByText('C')).toHaveAttribute('aria-hidden', 'true')
  })

  it('the same address is loaded only by a development build, where the local API serves it', async () => {
    vi.stubEnv('DEV', true)
    serve({ [CITY_CARE]: ok({ ...cityCareHospital, logo_url: HTTP_LOGO }) })
    open('/hospitals/city-care')
    await title('City Care Hospital')

    expect(screen.getByRole('img', { name: 'City Care Hospital logo' })).toHaveAttribute('src', HTTP_LOGO)
  })

  it('ATTACK — an https:// logo built to break out of the attribute stays one attribute of one image', async () => {
    vi.stubEnv('DEV', false)
    const hostile = 'https://cdn.example.test/a.png"onerror="window.pwned=1"x="'
    serve({ [CITY_CARE]: ok({ ...cityCareHospital, logo_url: hostile }) })
    open('/hospitals/city-care')
    await title('City Care Hospital')

    const images = document.querySelectorAll('img')
    expect(images).toHaveLength(1)
    expect(images[0].getAttributeNames().sort()).toEqual(['alt', 'class', 'decoding', 'height', 'referrerpolicy', 'src', 'width'])
    expect(new URL(images[0].src).origin).toBe('https://cdn.example.test')
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
  })

  it.each([
    ['the list', '/hospitals'],
    ['the doctors page', '/hospitals/city-care/doctors'],
    ['home', '/'],
  ])('%s never loads a logo at all, safe or not', async (_case, path) => {
    serve({ ...hospitalDirectory([cityCareHospital]), ...doctorDirectory('city-care', [ashaRao]), 'GET /me': ok(me([cityCare])) })
    open(path)

    await screen.findAllByText(/City Care Hospital/)
    expect(document.querySelector('img, picture, [src], [srcset], [style*="url"]')).toBeNull()
    expect(document.body.innerHTML).not.toContain('cdn.example.test')
  })
})

describe('SERVER TEXT IS TEXT — markup in any field is shown as the characters it is', () => {
  const INJECTED = 'script, iframe, object, embed, b, i, u, s, marquee, style, link, svg[onload], [onerror], [onclick], [onload], a[href^="javascript"]'

  const hostile = hospital({
    name: 'Evil<b>bold</b>',
    address: {
      line1: '<script>window.pwned=1</script>1 Road',
      line2: '<iframe src="javascript:window.pwned=1"></iframe>Block A',
      city: '<i>Bengaluru</i>',
      state: '<u>Karnataka</u>',
      postal_code: '<s>560038</s>',
      country: '<a href="javascript:window.pwned=1">India</a>',
    },
    phone: '<svg onload="window.pwned=1">080',
    timezone: '<img src=x onerror=window.pwned=1>',
    logo_url: null,
  })

  afterEach(() => {
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
  })

  it('ATTACK — every field of a hospital page carrying markup: shown literally, no element created', async () => {
    serve({ [CITY_CARE]: ok(hostile) })
    open('/hospitals/city-care')
    await title('Evil<b>bold</b>')

    for (const literal of [
      '<script>window.pwned=1</script>1 Road',
      '<iframe src="javascript:window.pwned=1"></iframe>Block A',
      '<i>Bengaluru</i>, <u>Karnataka</u> <s>560038</s>',
      '<a href="javascript:window.pwned=1">India</a>',
      '<svg onload="window.pwned=1">080',
      '<img src=x onerror=window.pwned=1>',
    ]) {
      expect(screen.getByText(literal)).toBeInTheDocument()
    }
    expect(document.body.querySelector(INJECTED)).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    // A phone that is not a number is no link of any kind.
    expect(main().querySelector('a[href^="tel:"]')).toBeNull()
    expect(document.title).toBe('Evil<b>bold</b> · Atheris Health')
  })

  it('ATTACK — the same hospital on a card, on its doctors page and among the cities', async () => {
    serve({ [LIST]: okPage([hostile]), [CITIES]: ok({ cities: ['<i>Bengaluru</i>', '"><script>window.pwned=1</script>'] }), [CITY_CARE]: ok(hostile), ...doctorDirectory('city-care', []) })
    const { router } = open()

    const listed = await card('Evil<b>bold</b>')
    expect(listed).toHaveAccessibleDescription('<i>Bengaluru</i>, <u>Karnataka</u> <svg onload="window.pwned=1">080')
    await waitFor(() =>
      expect(within(screen.getByRole('combobox', { name: 'City' })).getAllByRole('option').map((option) => option.textContent)).toEqual([
        'All cities',
        '<i>Bengaluru</i>',
        '"><script>window.pwned=1</script>',
      ]),
    )
    expect(document.body.querySelector(INJECTED)).toBeNull()

    await act(() => router.navigate('/hospitals/city-care/doctors'))

    expect(await title('Doctors at Evil<b>bold</b>')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to Evil<b>bold</b>' })).toHaveAttribute('href', '/hospitals/city-care')
    expect(document.body.querySelector(INJECTED)).toBeNull()
  })

  it('ATTACK — markup in the URL’s own search and city is shown in the field and the filter, never rendered', async () => {
    const api = serve(hospitalDirectory(THREE))
    const q = '<img src=x onerror=window.pwned=1>'
    const city = '"><script>window.pwned=1</script>'
    open(`/hospitals?q=${encodeURIComponent(q)}&city=${encodeURIComponent(city)}`)

    expect(await screen.findByText('No hospitals match your search')).toBeInTheDocument()
    expect(await searchField()).toHaveValue(q)
    expect(screen.getByRole('combobox', { name: 'City' })).toHaveValue(city)
    expect(document.body.querySelector(INJECTED)).toBeNull()
    expect(document.querySelector('img')).toBeNull()
    expect(api.calls(LIST)[0].params).toEqual({ page: 1, page_size: 20, search: q, city })
  })

  it('ATTACK — markup in a linked hospital’s name from /me is text on home', async () => {
    serve({ 'GET /me': ok(me([{ ...cityCare, hospital_name: '<img src=x onerror=window.pwned=1>City Care' }])) })
    open('/')

    expect(await card('<img src=x onerror=window.pwned=1>City Care')).toHaveAttribute('href', '/hospitals/city-care')
    expect(document.body.querySelector(INJECTED)).toBeNull()
    expect(document.querySelector('img')).toBeNull()
  })
})

describe('SEARCH — typing cannot flood the API, and the URL decides what is shown', () => {
  it('ATTACK — a burst of forty keystrokes is one request, for the final text only', async () => {
    const api = serve(hospitalDirectory(THREE))
    const { router } = open()
    const field = await searchField()
    await card('City Care Hospital')
    vi.useFakeTimers()

    const word = 'sunrise medical centre'
    for (let round = 0; round < 2; round++) {
      for (let length = 1; length <= word.length - 2; length++) {
        typed(field, word.slice(0, length))
        await pass(40)
      }
    }
    typed(field, word)
    expect(api.calls(LIST)).toHaveLength(1)
    expect(router.state.location.search).toBe('')

    await pass(SEARCH_DEBOUNCE_MS)
    await until(() => expect(api.calls(LIST)).toHaveLength(2))
    await pass(SEARCH_DEBOUNCE_MS * 3)

    expect(api.calls(LIST)).toHaveLength(2)
    expect(api.calls(LIST)[1].params).toEqual({ page: 1, page_size: 20, search: word })
    // No request per keystroke for the cities either.
    expect(api.calls(CITIES)).toHaveLength(1)
    expect(api.sent).toHaveLength(3)
  })

  it('typing and then putting the text back asks for nothing', async () => {
    const api = serve(hospitalDirectory(THREE))
    const { router } = open('/hospitals?q=lake')
    const field = await searchField()
    await card('Lakeside Clinic')
    vi.useFakeTimers()

    typed(field, 'lakes')
    await pass(SEARCH_DEBOUNCE_MS - 1)
    typed(field, 'lake')
    await pass(SEARCH_DEBOUNCE_MS * 3)

    expect(api.calls(LIST)).toHaveLength(1)
    expect(router.state.location.search).toBe('?q=lake')
  })

  it('a search still waiting when the patient leaves the page is dropped: no late request, no stray query on the next page', async () => {
    await import('@/pages/hospitals/HospitalDetailPage')
    const api = serve(hospitalDirectory(THREE))
    const { router } = open()
    const field = await searchField()
    const listed = await card('City Care Hospital')
    vi.useFakeTimers()

    typed(field, 'lake')
    await pass(SEARCH_DEBOUNCE_MS - 1)
    fireEvent.click(listed)
    await until(() => expect(screen.getByRole('heading', { level: 1, name: 'City Care Hospital' })).toBeInTheDocument())
    await pass(SEARCH_DEBOUNCE_MS * 3)

    expect(api.calls(LIST)).toHaveLength(1)
    expect(router.state.location.pathname).toBe('/hospitals/city-care')
    expect(router.state.location.search).toBe('')
  })

  it('the URL wins over text still being typed when it changes under the field', async () => {
    const api = serve(hospitalDirectory(THREE))
    const { router } = open()
    const field = await searchField()
    await card('City Care Hospital')
    vi.useFakeTimers()

    typed(field, 'lake')
    await pass(SEARCH_DEBOUNCE_MS - 1)
    // A link followed, a bookmark opened: not this page's own change.
    await act(() => router.navigate('/hospitals?q=sun&city=Bengaluru'))
    await until(() => expect(field).toHaveValue('sun'))
    await pass(SEARCH_DEBOUNCE_MS * 3)

    expect(router.state.location.search).toBe('?q=sun&city=Bengaluru')
    expect(field).toHaveValue('sun')
    expect(api.calls(LIST).map((request) => request.params)).toEqual([
      { page: 1, page_size: 20 },
      { page: 1, page_size: 20, search: 'sun', city: 'Bengaluru' },
    ])
    await until(() => expect(position()).toHaveTextContent(/^1 hospital$/))
    expect(screen.getByRole('link', { name: 'Sunrise Medical Centre' })).toBeInTheDocument()
  })

  it('what is shown is what the URL says: the list is never filtered by text that has not reached it', async () => {
    serve(hospitalDirectory(THREE))
    const { router } = open()
    const field = await searchField()
    await card('City Care Hospital')
    vi.useFakeTimers()

    typed(field, 'zzz')
    await pass(SEARCH_DEBOUNCE_MS - 1)

    // Typed, not yet searched: the URL, the count and the cards are still the unfiltered list.
    expect(router.state.location.search).toBe('')
    expect(position()).toHaveTextContent(/^3 hospitals$/)
    expect(within(screen.getByRole('list', { name: 'Hospitals' })).getAllByRole('link')).toHaveLength(3)

    await pass(1)
    await until(() => expect(position()).toHaveTextContent('No hospitals match'))
    expect(router.state.location.search).toBe('?q=zzz')
  })
})

describe('OFFLINE and SERVER FAILURE — told apart, in the app’s words, and Retry asks again', () => {
  const GATEWAY_PAGE: Outcome = {
    status: 503,
    data: '<html><body><h1>503 Service Temporarily Unavailable</h1><script>window.pwned=1</script>nginx</body></html>',
  }

  it.each([
    ['the list', '/hospitals', LIST, 'We could not load the hospitals. Please try again.'],
    ['a hospital', '/hospitals/city-care', CITY_CARE, 'We could not load this hospital. Please try again.'],
    ['the doctors page', '/hospitals/city-care/doctors', CITY_CARE, 'We could not load this hospital. Please try again.'],
  ])('on %s a gateway’s 503 page is a server failure — not "No connection", and none of it is shown', async (_case, path, route, message) => {
    const api = serve({ ...hospitalDirectory(THREE), ...doctorDirectory('city-care', [ashaRao]), [route]: GATEWAY_PAGE })
    const { user } = open(path)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(message)
    expect(document.body).not.toHaveTextContent(/No connection|internet connection/)
    expect(document.body).not.toHaveTextContent(/503|Service Temporarily|nginx|Network Error|Request failed/)
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()

    // Each press asks exactly once more; a second failure is shown as one, not swallowed.
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(api.calls(route)).toHaveLength(2))
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled())

    // The connection drops: now, and only now, the app says so.
    api.on({ [route]: unreachable })
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No connection'))
    expect(screen.getByRole('alert')).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
    expect(screen.queryByText(message)).not.toBeInTheDocument()
    expect(api.calls(route)).toHaveLength(3)

    api.on(hospitalDirectory(THREE))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    await screen.findAllByText(/City Care Hospital/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(api.calls(route)).toHaveLength(4)
  })

  it('a failure is never dressed up as an empty result: no "no hospitals" wording, no count, no cards', async () => {
    serve({ ...hospitalDirectory(THREE), [LIST]: fail(500, 'INTERNAL_ERROR', 'server wording') })
    open('/hospitals?q=care')

    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/No hospitals|nothing on this page|0 hospitals/i)
    expect(position()).toBeEmptyDOMElement()
    expect(screen.queryByRole('list', { name: 'Hospitals' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear search and city' })).not.toBeInTheDocument()
    // The search that failed is still in the field, to be tried again.
    expect(await searchField()).toHaveValue('care')
  })

  it.each([
    ['without its pagination', { success: true, message: 'ok', data: THREE, metadata: { request_id: 'r' } }],
    ['whose data is not a list', { success: true, message: 'ok', data: { hospitals: THREE }, metadata: { pagination: { page: 1, page_size: 20, total_records: 3, total_pages: 1 } } }],
    ['whose data is missing', { success: true, message: 'ok', data: null, metadata: { pagination: { page: 1, page_size: 20, total_records: 3, total_pages: 1 } } }],
  ])('a 200 %s is a failure with a retry, not a broken page and not an invented count', async (_case, body) => {
    const api = serve({ ...hospitalDirectory(THREE), [LIST]: { status: 200, data: body } })
    const { user } = open()

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load the hospitals. Please try again.')
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
    expect(position()).toBeEmptyDOMElement()

    api.on(hospitalDirectory(THREE))
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await card('City Care Hospital')).toBeInTheDocument()
  })
})

describe('SESSION — a 401 on a discovery call refreshes once, and a refused refresh signs out cleanly', () => {
  /**
   * Answers only the refreshed token; the old one is refused, as an expired
   * token is. `seen` keeps what each request carried when it arrived — a retry
   * reuses its request object, so the header cannot be read back afterwards.
   */
  const onlyFor = (token: string, answer: Outcome, seen: string[] = []): Handler => (config) => {
    seen.push(`${config.url} ${headerOf(config, 'Authorization')}`)
    return headerOf(config, 'Authorization') === `Bearer ${token}` ? answer : fail(401, 'AUTHENTICATION_REQUIRED', 'server wording')
  }

  it('an expired token on the list and the cities together: one refresh, both retried with the new token, the list shown', async () => {
    signIn('access-old')
    const seen: string[] = []
    const api = serve({
      [LIST]: onlyFor('access-new', okPage(THREE), seen),
      [CITIES]: onlyFor('access-new', ok({ cities: ['Bengaluru', 'Mysuru'] }), seen),
      [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }),
    })
    const { router } = renderApp('/hospitals?q=care')

    expect(await card('City Care Hospital')).toBeInTheDocument()
    await waitFor(() => expect(within(screen.getByRole('combobox', { name: 'City' })).getAllByRole('option')).toHaveLength(3))

    expect(api.calls(REFRESH)).toHaveLength(1)
    // The refresh is the cookie's business: no bearer token goes with it.
    expect(headerOf(api.calls(REFRESH)[0], 'Authorization')).toBeUndefined()
    // Each call was refused once with the old token and retried once with the new: four requests, no more.
    expect([...seen].sort()).toEqual([
      '/hospital-cities Bearer access-new',
      '/hospital-cities Bearer access-old',
      '/hospitals Bearer access-new',
      '/hospitals Bearer access-old',
    ])
    // The retry is the same question, on the same page.
    expect(api.calls(LIST)[1].params).toEqual({ page: 1, page_size: 20, search: 'care' })
    expect(router.state.location.pathname + router.state.location.search).toBe('/hospitals?q=care')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('server wording')
    expect(isSignedIn()).toBe(true)
  })

  it.each([
    ['a hospital', '/hospitals/city-care'],
    ['the doctors page', '/hospitals/city-care/doctors'],
  ])('ATTACK — a dead session opening %s: one refresh, one sign-out, nothing of the hospital shown', async (_case, path) => {
    signIn('access-old')
    const api = serve({ [CITY_CARE]: fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'), [REFRESH]: fail(401, 'UNAUTHORIZED') })
    const { router } = renderApp(path)

    expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(isSignedIn()).toBe(false)
    expect(api.calls(REFRESH)).toHaveLength(1)
    // Asked once with the dead token and never again: no retry loop.
    expect(api.calls(CITY_CARE)).toHaveLength(1)
    expect(document.body).not.toHaveTextContent(/City Care|server wording|could not load/)
  })

  it('ATTACK — a session that dies mid-browse leaves no hospital behind: the cache is emptied and nothing more is asked', async () => {
    signIn('access-old')
    const api = serve(hospitalDirectory(THREE))
    const { user, router } = renderApp('/hospitals')
    const listed = await card('City Care Hospital')
    await waitFor(() => expect(queryClient.getQueryCache().getAll().length).toBeGreaterThanOrEqual(2))

    api.on({ [CITY_CARE]: fail(401, 'AUTHENTICATION_REQUIRED'), [REFRESH]: fail(401, 'UNAUTHORIZED') })
    await user.click(listed)

    expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(isSignedIn()).toBe(false)
    // The next person to sign in on this device starts with none of this patient's answers.
    expect(queryClient.getQueryCache().getAll()).toEqual([])
    expect(document.body).not.toHaveTextContent(/City Care|Lakeside|Sunrise|Linked/)

    const sentAtSignOut = api.sent.length
    await act(() => new Promise((resolve) => setTimeout(resolve, 50)))
    expect(api.sent).toHaveLength(sentAtSignOut)
    expect(api.calls(REFRESH)).toHaveLength(1)
  })

  it('a refresh that cannot be reached is not a sign-out: the patient stays, is told it did not load, and Retry recovers', async () => {
    signIn('access-old')
    const api = serve({
      [LIST]: onlyFor('access-new', okPage(THREE)),
      [CITIES]: onlyFor('access-new', ok({ cities: [] })),
      [REFRESH]: unreachable,
    })
    const { user, router } = renderApp('/hospitals')

    expect(await screen.findByRole('alert')).toHaveTextContent('We could not load the hospitals. Please try again.')
    expect(router.state.location.pathname).toBe('/hospitals')
    expect(isSignedIn()).toBe(true)
    expect(document.body).not.toHaveTextContent('server wording')

    api.on({ [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }) })
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await card('City Care Hospital')).toBeInTheDocument()
    expect(isSignedIn()).toBe(true)
  })
})

describe('KEYBOARD and SCREEN READER — the basics of using discovery without a pointer or a screen', () => {
  it('the count is one live region that stays in the page and changes its text: loading, results, a new filter', async () => {
    let answer: (outcome: Outcome) => void = () => {}
    const pending = new Promise<Outcome>((resolve) => (answer = resolve))
    const api = serve({ ...hospitalDirectory(THREE), [LIST]: () => pending })
    const { user } = open()

    const live = await screen.findByText('Loading hospitals…')
    expect(live).toHaveAttribute('role', 'status')
    expect(live.closest('[aria-hidden="true"]')).toBeNull()

    api.on(hospitalDirectory(THREE))
    answer(okPage(THREE))
    await card('City Care Hospital')

    // The same node, so the change is announced; a replaced node would not be.
    expect(screen.getAllByRole('status')).toEqual([live])
    expect(live).toHaveTextContent(/^3 hospitals$/)

    await waitFor(() => expect(within(screen.getByRole('combobox', { name: 'City' })).getAllByRole('option')).toHaveLength(3))
    await user.selectOptions(screen.getByRole('combobox', { name: 'City' }), 'Mysuru')

    await waitFor(() => expect(live).toHaveTextContent(/^1 hospital$/))
    expect(screen.getAllByRole('status')).toEqual([live])
    expect(live).toBeInTheDocument()
  })

  it('a failure is announced as an alert, and the live count does not go on claiming a result', async () => {
    serve({ ...hospitalDirectory(THREE), [LIST]: unreachable })
    open()

    const alert = await screen.findByRole('alert')
    expect(main()).toContainElement(alert)
    expect(position()).toBeEmptyDOMElement()
  })

  it('works from the keyboard alone: search with Enter, reach the filter and a card with Tab, open it with Enter', async () => {
    const api = serve(hospitalDirectory(THREE))
    const { user, router } = open()
    const field = await searchField()
    await card('City Care Hospital')

    for (let presses = 0; presses < 10 && document.activeElement !== field; presses++) await user.tab()
    expect(field).toHaveFocus()

    await user.keyboard('lake{Enter}')

    expect(await card('Lakeside Clinic')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('link', { name: 'City Care Hospital' })).not.toBeInTheDocument())
    expect(router.state.location.search).toBe('?q=lake')
    expect(api.calls(LIST).at(-1)?.params).toEqual({ page: 1, page_size: 20, search: 'lake' })
    // Searching did not take the field away from the patient.
    expect(field).toHaveFocus()

    // Tab order is reading order: clear, city, then the results.
    await user.tab()
    expect(screen.getByRole('button', { name: 'Clear search' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('combobox', { name: 'City' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'Lakeside Clinic' })).toHaveFocus()

    await user.keyboard('{Enter}')

    expect(await title('Lakeside Clinic')).toHaveFocus()
    expect(router.state.location.pathname).toBe('/hospitals/lakeside-clinic')

    // From the heading, the next stops are the page's own actions, in order.
    await user.tab()
    expect(screen.getByRole('link', { name: 'View Doctors' })).toHaveFocus()
    await user.tab()
    expect(screen.getByRole('link', { name: 'Link my record' })).toHaveFocus()
  })

  it.each([
    ['the list', '/hospitals?q=clinic'],
    ['a hospital', '/hospitals/city-care'],
    ['the doctors page', '/hospitals/city-care/doctors'],
    ['a doctor', `/hospitals/city-care/doctors/${ashaRao.ref}`],
    ['the availability page', `/hospitals/city-care/doctors/${ashaRao.ref}/availability`],
    ['a later week of availability, with a slot chosen', `/hospitals/city-care/doctors/${ashaRao.ref}/availability?date=2026-10-15&slot=2026-10-15T09%3A00%3A00%2B05%3A30`],
    ['the booking page', `/hospitals/city-care/doctors/${ashaRao.ref}/book?date=2026-10-09&start=2026-10-09T10%3A15%3A00%2B05%3A30&end=2026-10-09T10%3A30%3A00%2B05%3A30`],
    ['the booking page with a link that is not valid', `/hospitals/city-care/doctors/${ashaRao.ref}/book?date=yesterday`],
  ])('on %s nothing jumps the tab order, every icon is hidden from a screen reader and every control has a name', async (_case, path) => {
    serve({ ...hospitalDirectory(THREE), ...doctorDirectory('city-care', [ashaRao, vikramShah]) })
    open(path)
    await screen.findAllByText(/City Care Hospital|Lakeside Clinic/)
    await waitFor(() => expect(main().querySelector('[data-slot="skeleton"]')).toBeNull())

    // A heading may take focus from script (-1); nothing is given a place in the order by hand.
    for (const element of document.querySelectorAll('[tabindex]')) expect(element).toHaveAttribute('tabindex', '-1')
    for (const icon of document.querySelectorAll('svg')) expect(icon).toHaveAttribute('aria-hidden', 'true')
    for (const image of document.querySelectorAll('img')) expect(image.getAttribute('alt')).toMatch(/\S/)
    for (const control of document.querySelectorAll<HTMLElement>('a, button, input, select')) expect(control).toHaveAccessibleName()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(document.querySelectorAll('main')).toHaveLength(1)
  })

  it('after Retry on the list, focus is at the top of the results, not lost with the button that was pressed', async () => {
    const api = serve({ ...hospitalDirectory(THREE), [LIST]: fail(500, 'INTERNAL_ERROR') })
    const { user } = open()
    await screen.findByRole('alert')

    api.on(hospitalDirectory(THREE))
    await user.click(screen.getByRole('button', { name: 'Try again' }))

    await card('City Care Hospital')
    expect(screen.getByRole('heading', { level: 2, name: 'Hospitals' })).toHaveFocus()
    expect(document.body).not.toHaveFocus()
  })

  it('a card that leads to a hospital no longer available lands focus on the heading that says so', async () => {
    serve({ ...hospitalDirectory(THREE), [CITY_CARE]: fail(404, 'RESOURCE_NOT_FOUND', 'server wording') })
    const { user } = open()

    await user.click(await card('City Care Hospital'))

    expect(await title('This hospital is not available')).toHaveFocus()
    expect(document.body).not.toHaveTextContent('server wording')
  })
})
