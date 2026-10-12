import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SEARCH_DEBOUNCE_MS } from '@/pages/hospitals/HospitalsPage'
import { deferred, fail, headerOf, ok, okPage, serve, unreachable } from '@/test/fakeApi'
import {
  cityCareHospital,
  hospital,
  lakesideHospital,
  manyHospitals,
  promotedHospital,
  sunriseHospital,
} from '@/test/fixtures'
import { hospitalDirectory } from '@/test/hospitalDirectory'
import { renderApp, signIn } from '@/test/renderApp'

const LIST = 'GET /hospitals'
const CITIES = 'GET /hospital-cities'
const FIRST_PAGE = { page: 1, page_size: 20 }

/** City Care (Bengaluru, linked), Lakeside Clinic (Mysuru), Sunrise Medical Centre (Bengaluru). */
const THREE = [cityCareHospital, lakesideHospital, sunriseHospital]

function open(path = '/hospitals') {
  signIn('access-1')
  return renderApp(path)
}

const searchField = () => screen.findByRole('searchbox', { name: 'Search by hospital name' })
const cityFilter = () => screen.getByRole('combobox', { name: 'City' })
const card = (name: string | RegExp) => screen.findByRole('link', { name })
const cards = () => within(screen.getByRole('list', { name: 'Hospitals' })).getAllByRole('link')
/** The live line under "Hospitals": the count, or where the patient is in it. */
const position = () => screen.getByRole('status')
const lastListRequest = (api: ReturnType<typeof serve>) => api.calls(LIST).at(-1)

/**
 * The search delay is stepped through with the clock stopped (`vi.useFakeTimers`),
 * so "not yet" and "now" are exact. While it is stopped nothing waits on real
 * time: input is fired directly, `pass` moves the clock, and `until` lets the
 * app catch up — promises settle, zero-delay timers run — without moving it.
 */
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

describe('hospital discovery — the list', () => {
  it('shows skeletons while the first page loads, then the hospitals', async () => {
    const { handler, answer } = deferred()
    serve({ ...hospitalDirectory(THREE), [LIST]: handler })
    open()

    expect(await screen.findByText('Loading hospitals…')).toHaveAttribute('role', 'status')
    expect(document.querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByRole('list', { name: 'Hospitals' })).not.toBeInTheDocument()

    answer(okPage(THREE))

    expect(await card('City Care Hospital')).toBeInTheDocument()
    expect(document.querySelector('[data-slot="skeleton"]')).not.toBeInTheDocument()
    expect(screen.queryByText('Loading hospitals…')).not.toBeInTheDocument()
  })

  it('lists what the server returns, in its order, asking only for a page', async () => {
    const api = serve(hospitalDirectory(THREE))
    open()
    await card('City Care Hospital')

    const [first, second, third] = cards()
    expect(cards()).toHaveLength(3)
    expect(first).toHaveAccessibleName('City Care Hospital')
    expect(second).toHaveAccessibleName('Lakeside Clinic')
    expect(third).toHaveAccessibleName('Sunrise Medical Centre')
    expect(position()).toHaveTextContent(/^3 hospitals$/)
    // Everything fits on one page: no page controls to mislead.
    expect(screen.queryByRole('navigation', { name: 'Pages of hospitals' })).not.toBeInTheDocument()

    // The request names no account, patient or hospital — the token says who is asking.
    const [request] = api.calls(LIST)
    expect(api.calls(LIST)).toHaveLength(1)
    expect(request.params).toEqual(FIRST_PAGE)
    expect(headerOf(request, 'Authorization')).toBe('Bearer access-1')
    expect(api.calls(CITIES)).toHaveLength(1)
    expect(api.calls(CITIES)[0].params).toBeUndefined()
    expect(api.sent).toHaveLength(2)
    expect(document.title).toBe('Find a hospital · Atheris Health')
  })

  it('shows on a card only the name, the place, the phone and a "Linked" mark', async () => {
    serve(hospitalDirectory(THREE))
    open()

    const cityCare = await card('City Care Hospital')
    expect(cityCare).toHaveAttribute('href', '/hospitals/city-care')
    expect(cityCare).toHaveAccessibleDescription('Bengaluru, Karnataka +91 80 5550 0100 Linked')

    // No phone and no link: neither a blank line nor a mark is invented.
    const lakeside = screen.getByRole('link', { name: 'Lakeside Clinic' })
    expect(lakeside).toHaveAccessibleDescription('Mysuru, Karnataka')
    expect(within(lakeside).queryByText('Linked')).not.toBeInTheDocument()

    // A card carries no logo and nothing from the full address.
    expect(within(screen.getByRole('main')).queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('main')).not.toHaveTextContent(/12 MG Road|560038|Asia\/Kolkata|null|undefined/)
  })

  it('falls back to the first address line when a hospital has no city or state', async () => {
    const noCity = hospital({
      ref: 'ring-road',
      name: 'Ring Road Clinic',
      address: { line1: 'Plot 9, Ring Road', line2: 'Near the flyover', city: null, state: null, postal_code: null, country: null },
      phone: null,
    })
    serve(hospitalDirectory([noCity]))
    open()

    expect(await card('Ring Road Clinic')).toHaveAccessibleDescription('Plot 9, Ring Road')
    expect(position()).toHaveTextContent(/^1 hospital$/)
  })

  it('ATTACK — a response carrying more than the contract allows: only the allow-listed fields reach the screen, and markup stays text', async () => {
    const leaky = {
      ...hospital({ name: '<img src=x onerror="window.pwned=1">Evil & Sons', logo_url: 'javascript:window.pwned=1' }),
      id: '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55',
      settings: { 'feature.patient_app.enabled': true, api_key: 'SETTINGS-SECRET' },
      tax_id: 'TAX-SECRET-99',
      email: 'admin@secret.example',
      currency: 'XSECRETCUR',
      is_active: true,
      created_by: 'STAFF-SECRET',
      patient_count: 987654,
      distance_km: 4.2,
    }
    serve({ [LIST]: okPage([leaky]), [CITIES]: ok({ cities: ['<b>Bengaluru</b>'] }) })
    open()

    const evil = await card(/Evil & Sons/)
    expect(evil).toHaveTextContent('<img src=x onerror="window.pwned=1">Evil & Sons')
    expect(document.querySelector('main img')).toBeNull()
    expect(document.querySelector('main b')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()

    const shown = document.body.textContent ?? ''
    for (const secret of ['5f0c2a9e', 'SETTINGS-SECRET', 'TAX-SECRET-99', 'secret.example', 'XSECRETCUR', 'STAFF-SECRET', '987654']) {
      expect(shown).not.toContain(secret)
    }
    // There is no distance to show: the app does not know where the patient is.
    expect(shown).not.toMatch(/4\.2|\bkm\b|distance|miles|away|near you/i)
    expect(document.body.innerHTML).not.toContain('javascript:')
  })

  it('SPONSORED — a promoted listing is labelled, and no other hospital ever is', async () => {
    serve(hospitalDirectory([cityCareHospital, promotedHospital, lakesideHospital]))
    open()

    const promoted = await card('Harbour Health')
    expect(within(promoted).getByText('Sponsored')).toBeInTheDocument()
    expect(promoted).toHaveAccessibleDescription(/Sponsored/)

    // The label is the only thing promotion changes: the server's order stands.
    expect(cards().map((each) => each.getAttribute('href'))).toEqual([
      '/hospitals/city-care',
      '/hospitals/harbour-health',
      '/hospitals/lakeside-clinic',
    ])
    expect(screen.getAllByText('Sponsored')).toHaveLength(1)
  })

  it('shows no "Sponsored" label at all for standard listings — which is every real hospital', async () => {
    serve(hospitalDirectory(THREE))
    open()
    await card('City Care Hospital')

    expect(screen.queryByText(/sponsored|promoted|featured|\bad\b/i)).not.toBeInTheDocument()
  })

  it('is labelled for assistive technology: a search landmark, named fields, a live count', async () => {
    serve(hospitalDirectory(THREE))
    open()
    await card('City Care Hospital')

    const form = screen.getByRole('search', { name: 'Find a hospital' })
    const field = within(form).getByRole('searchbox', { name: 'Search by hospital name' })
    expect(field).toHaveAttribute('type', 'search')
    expect(field).toHaveAttribute('maxlength', '80')
    expect(field).toHaveAttribute('enterkeyhint', 'search')
    expect(within(form).getByRole('combobox', { name: 'City' })).toBeInTheDocument()

    // `role="status"` is a polite live region: a new count is read out.
    expect(position()).toHaveTextContent('3 hospitals')
    expect(screen.getByRole('region', { name: 'Hospitals' })).toContainElement(position())
    expect(screen.getByRole('heading', { level: 1, name: 'Find a hospital' })).toBeInTheDocument()
  })

  it('RESPONSIVE — one column of cards with touch-sized controls: no table, nothing under 44 px', async () => {
    const longName = hospital({ ref: 'long', name: `Clinic 00 ${'Superspeciality'.repeat(8)}`, phone: null })
    serve(hospitalDirectory([longName, ...manyHospitals(45)]))
    open('/hospitals?q=clinic')
    const long = await card(/Superspeciality/)

    const main = screen.getByRole('main')
    expect(within(main).queryByRole('table')).not.toBeInTheDocument()
    // A name with no spaces wraps instead of pushing the page sideways.
    expect(within(long).getByText(/Superspeciality/)).toHaveClass('break-words')

    const touchSized = /(^|\s)(h-11|min-h-11|size-11|min-h-20)(\s|$)/
    const controls = [...main.querySelectorAll('a, button, input, select')]
    // 20 cards, the field and its clear button, the city filter, two page buttons.
    expect(controls).toHaveLength(25)
    for (const control of controls) expect(control.className).toMatch(touchSized)

    const navigation = within(screen.getByRole('navigation', { name: 'Main' })).getAllByRole('link')
    expect(navigation.map((link) => link.textContent)).toEqual(['Home', 'Hospitals', 'Appointments', 'Link hospital'])
    for (const link of navigation) expect(link).toHaveClass('min-h-14')
    expect(screen.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')
  })

  it('returns to sign-in when the session has ended, refreshing once for both requests', async () => {
    signIn('access-old')
    const api = serve({
      [LIST]: fail(401, 'AUTHENTICATION_REQUIRED'),
      [CITIES]: fail(401, 'AUTHENTICATION_REQUIRED'),
      'POST /auth/refresh': fail(401, 'UNAUTHORIZED'),
    })
    const { router } = renderApp('/hospitals')

    expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(api.calls('POST /auth/refresh')).toHaveLength(1)
  })

  describe('search', () => {
    it('waits for typing to pause, then asks once and puts the search in the URL', async () => {
      const api = serve(hospitalDirectory(THREE))
      const { router } = open('/hospitals?page=2')
      const field = await searchField()
      await screen.findByText('There is nothing on this page')
      field.focus()
      vi.useFakeTimers()

      // Each keystroke starts the wait again: more than the delay passes in all, and nothing is asked.
      for (const soFar of ['L', 'LA', 'LAK', 'LAKE']) {
        typed(field, soFar)
        await pass(SEARCH_DEBOUNCE_MS - 1)
      }
      expect(api.calls(LIST)).toHaveLength(1)
      expect(router.state.location.search).toBe('?page=2')

      await pass(1)

      // One request for the whole word, back on the first page.
      await until(() => expect(api.calls(LIST)).toHaveLength(2))
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, search: 'LAKE' })
      expect(router.state.location.search).toBe('?q=LAKE')

      await until(() => expect(screen.getByRole('link', { name: 'Lakeside Clinic' })).toBeInTheDocument())
      expect(screen.queryByRole('link', { name: 'City Care Hospital' })).not.toBeInTheDocument()
      expect(position()).toHaveTextContent(/^1 hospital$/)
      // The patient is still typing: the field keeps its text and the focus.
      expect(field).toHaveValue('LAKE')
      expect(field).toHaveFocus()
      await pass(SEARCH_DEBOUNCE_MS * 3)
      expect(api.calls(LIST)).toHaveLength(2)
    })

    it('searches at once on Enter, and the pause that follows does not ask again', async () => {
      const api = serve(hospitalDirectory(THREE))
      const { router } = open()
      const field = await searchField()
      await card('City Care Hospital')
      vi.useFakeTimers()

      typed(field, '  sun  ')
      fireEvent.submit(screen.getByRole('search'))

      // No time has passed at all. The text is trimmed before it is sent.
      await until(() => expect(api.calls(LIST)).toHaveLength(2))
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, search: 'sun' })
      expect(router.state.location.search).toBe('?q=sun')

      await pass(SEARCH_DEBOUNCE_MS * 3)

      expect(api.calls(LIST)).toHaveLength(2)
      expect(screen.getByRole('link', { name: 'Sunrise Medical Centre' })).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'City Care Hospital' })).not.toBeInTheDocument()
    })

    it('does not search for spaces, or for the search already shown', async () => {
      const api = serve(hospitalDirectory(THREE))
      const { router } = open('/hospitals?q=lake')
      const field = await searchField()
      await card('Lakeside Clinic')
      vi.useFakeTimers()

      typed(field, '  lake  ')
      fireEvent.submit(screen.getByRole('search'))
      await pass(SEARCH_DEBOUNCE_MS * 3)

      expect(api.calls(LIST)).toHaveLength(1)
      expect(router.state.location.search).toBe('?q=lake')
    })

    it('clears with one press, keeping the field in hand', async () => {
      serve(hospitalDirectory(THREE))
      const { user, router } = open('/hospitals?q=lake')
      const field = await searchField()
      expect(field).toHaveValue('lake')
      await card('Lakeside Clinic')
      expect(screen.queryByRole('link', { name: 'City Care Hospital' })).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Clear search' }))

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(field).toHaveValue('')
      expect(field).toHaveFocus()
      // Nothing left to clear.
      expect(screen.queryByRole('button', { name: 'Clear search' })).not.toBeInTheDocument()
    })

    it('RELOAD — the search, the city and the page all come back from the URL', async () => {
      const api = serve(hospitalDirectory(manyHospitals(45)))
      open('/hospitals?q=clinic&city=Bengaluru&page=2')

      expect(await card('Clinic 21')).toBeInTheDocument()
      expect(await searchField()).toHaveValue('clinic')
      await waitFor(() => expect(cityFilter()).toHaveValue('Bengaluru'))
      expect(position()).toHaveTextContent('Showing 21–40 of 45 hospitals')
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual({ page: 2, page_size: 20, search: 'clinic', city: 'Bengaluru' })
    })

    it('BACK and FORWARD — the field follows the URL, and the list with it', async () => {
      serve(hospitalDirectory(THREE))
      const { user, router } = open('/hospitals?q=lake')
      const field = await searchField()
      await card('Lakeside Clinic')

      // "Hospitals" in the navigation starts the list over…
      await user.click(screen.getByRole('link', { name: 'Hospitals' }))
      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(field).toHaveValue('')

      // …back returns to the search, forward to the full list.
      await act(() => router.navigate(-1))
      await waitFor(() => expect(field).toHaveValue('lake'))
      expect(router.state.location.search).toBe('?q=lake')
      expect(screen.queryByRole('link', { name: 'City Care Hospital' })).not.toBeInTheDocument()

      await act(() => router.navigate(1))
      await waitFor(() => expect(field).toHaveValue(''))
      expect(await card('City Care Hospital')).toBeInTheDocument()
    })

    it('BACK onto a search this page made itself still puts that search in the field', async () => {
      serve(hospitalDirectory(manyHospitals(45)))
      const { router } = open()
      const field = await searchField()
      await card('Clinic 01')
      vi.useFakeTimers()
      const search = (term: string) => {
        typed(field, term)
        fireEvent.submit(screen.getByRole('search'))
      }

      // A search, its second page, then a narrower search made from that page.
      search('clinic')
      await until(() => expect(router.state.location.search).toBe('?q=clinic'))
      await until(() => expect(screen.getByRole('button', { name: 'Next' })).toBeEnabled())
      fireEvent.click(screen.getByRole('button', { name: 'Next' }))
      await until(() => expect(router.state.location.search).toBe('?q=clinic&page=2'))
      search('clinic 0')
      await until(() => expect(router.state.location.search).toBe('?q=clinic+0'))

      await act(() => router.navigate(-1))

      // Back is the first search again — in the URL, in the field, and it stays that way.
      await until(() => expect(field).toHaveValue('clinic'))
      await pass(SEARCH_DEBOUNCE_MS * 3)
      expect(router.state.location.search).toBe('?q=clinic')
      expect(field).toHaveValue('clinic')
      expect(position()).toHaveTextContent('Showing 1–20 of 45 hospitals')
    })

    it('does not bring a search back after the navigation has started the list over', async () => {
      const api = serve(hospitalDirectory(THREE))
      const { router } = open('/hospitals?q=lake')
      const field = await searchField()
      await card('Lakeside Clinic')
      vi.useFakeTimers()

      fireEvent.click(screen.getByRole('link', { name: 'Hospitals' }))
      await until(() => expect(field).toHaveValue(''))
      await pass(SEARCH_DEBOUNCE_MS * 3)

      expect(router.state.location.search).toBe('')
      expect(field).toHaveValue('')
      expect(lastListRequest(api)?.params).toEqual(FIRST_PAGE)
    })
  })

  describe('city filter', () => {
    it('offers the cities the server lists and filters by the one chosen', async () => {
      const api = serve(hospitalDirectory(THREE))
      const { user, router } = open('/hospitals?page=2')
      await screen.findByText('There is nothing on this page')
      const filter = cityFilter()
      await waitFor(() =>
        expect(within(filter).getAllByRole('option').map((option) => option.textContent)).toEqual([
          'All cities',
          'Bengaluru',
          'Mysuru',
        ]),
      )
      expect(filter).toHaveValue('')

      await user.selectOptions(filter, 'Bengaluru')

      expect(await card('Sunrise Medical Centre')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'Lakeside Clinic' })).not.toBeInTheDocument()
      expect(position()).toHaveTextContent(/^2 hospitals$/)
      // A new filter starts from the first page.
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, city: 'Bengaluru' })
      expect(router.state.location.search).toBe('?city=Bengaluru')

      await user.selectOptions(filter, 'All cities')

      expect(await card('Lakeside Clinic')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(lastListRequest(api)?.params).toEqual(FIRST_PAGE)
    })

    it('combines with the search, and shows the URL’s city as chosen whatever its capitals', async () => {
      const api = serve(hospitalDirectory(THREE))
      open('/hospitals?q=care&city=bengaluru')

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(cards()).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: 'care', city: 'bengaluru' })
      await waitFor(() => expect(cityFilter()).toHaveValue('Bengaluru'))
    })

    it('offers only names as cities, whatever else the response carries', async () => {
      serve({ ...hospitalDirectory(THREE), [CITIES]: ok({ cities: ['Bengaluru', 42, null, '   ', { name: 'Mysuru' }, 'Mysuru'] }) })
      open()
      await card('City Care Hospital')

      await waitFor(() =>
        expect(within(cityFilter()).getAllByRole('option').map((option) => option.textContent)).toEqual([
          'All cities',
          'Bengaluru',
          'Mysuru',
        ]),
      )
    })

    it('still lists hospitals when the cities are not a list at all', async () => {
      serve({ ...hospitalDirectory(THREE), [CITIES]: ok({ cities: 'Bengaluru' }) })
      open()

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(within(cityFilter()).getAllByRole('option')).toHaveLength(1)
    })

    it('still lists hospitals when the cities cannot be loaded', async () => {
      serve({ ...hospitalDirectory(THREE), [CITIES]: fail(500, 'INTERNAL_ERROR', 'Traceback…') })
      open()

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(within(cityFilter()).getAllByRole('option').map((option) => option.textContent)).toEqual(['All cities'])
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })

  describe('the address bar', () => {
    it.each([
      ['zero', 'page=0'],
      ['a negative number', 'page=-3'],
      ['a fraction', 'page=2.5'],
      ['words', 'page=two'],
      ['SQL', "page=1;DROP TABLE hospitals"],
      ['beyond the last page the API accepts', 'page=1001'],
      ['nothing', 'page='],
    ])('ATTACK — a hand-edited page of %s: the first page is asked for, never a validation error', async (_case, query) => {
      const api = serve(hospitalDirectory(THREE))
      open(`/hospitals?${query}`)

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual(FIRST_PAGE)
    })

    it('ATTACK — over-long search and city in the URL are cut to what the API accepts before anything is sent', async () => {
      const api = serve(hospitalDirectory(THREE))
      open(`/hospitals?q=${'a'.repeat(500)}&city=${'b'.repeat(500)}`)

      expect(await screen.findByText('No hospitals match your search')).toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: 'a'.repeat(80), city: 'b'.repeat(80) })
    })

    it('ATTACK — filters the server does not offer are not forwarded to it', async () => {
      const api = serve(hospitalDirectory(THREE))
      open('/hospitals?include_inactive=1&is_active=false&hospital_id=5f0c2a9e&sort=id&page_size=5000&search=x')

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(api.calls(LIST)[0].params).toEqual(FIRST_PAGE)
    })

    it('sends wildcard characters as the text they are', async () => {
      const api = serve(hospitalDirectory(THREE))
      open(`/hospitals?q=${encodeURIComponent('100%_\\')}`)

      expect(await screen.findByText('No hospitals match your search')).toBeInTheDocument()
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: '100%_\\' })
      expect(await searchField()).toHaveValue('100%_\\')
    })
  })

  describe('empty states', () => {
    it('NONE AT ALL — says no hospital is available, with nothing to clear', async () => {
      serve(hospitalDirectory([]))
      open()

      expect(await screen.findByText('No hospitals to show yet')).toBeInTheDocument()
      expect(screen.getByText('No hospital is available in the app right now. Please check again later.')).toBeInTheDocument()
      expect(position()).toHaveTextContent('No hospitals to show')
      expect(screen.queryByText('No hospitals match your search')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Clear search and city' })).not.toBeInTheDocument()
      expect(screen.queryByRole('list', { name: 'Hospitals' })).not.toBeInTheDocument()
    })

    it('NOTHING MATCHES — says so, differently, and clears back to every hospital', async () => {
      serve(hospitalDirectory(THREE))
      const { user, router } = open('/hospitals?q=zzz&city=Mysuru')

      expect(await screen.findByText('No hospitals match your search')).toBeInTheDocument()
      expect(position()).toHaveTextContent('No hospitals match')
      expect(screen.queryByText('No hospitals to show yet')).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Clear search and city' }))

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(cards()).toHaveLength(3)
      expect(router.state.location.search).toBe('')
      expect(await searchField()).toHaveValue('')
      expect(await searchField()).toHaveFocus()
      expect(cityFilter()).toHaveValue('')
    })

    it('keeps a city in force visible even when the server no longer lists it', async () => {
      serve(hospitalDirectory(THREE))
      open('/hospitals?city=Atlantis')

      expect(await screen.findByText('No hospitals match your search')).toBeInTheDocument()
      await waitFor(() => expect(within(cityFilter()).getAllByRole('option')).toHaveLength(4))
      expect(cityFilter()).toHaveValue('Atlantis')
    })
  })

  describe('pagination', () => {
    const nextPage = () => screen.getByRole('button', { name: 'Next' })
    const previousPage = () => screen.getByRole('button', { name: 'Previous' })
    const pager = () => screen.getByRole('navigation', { name: 'Pages of hospitals' })

    it('pages through the results, saying truthfully where the patient is', async () => {
      const api = serve(hospitalDirectory(manyHospitals(45)))
      const { user, router } = open()

      expect(await card('Clinic 01')).toBeInTheDocument()
      expect(cards()).toHaveLength(20)
      expect(position()).toHaveTextContent('Showing 1–20 of 45 hospitals')
      expect(within(pager()).getByText('Page 1 of 3')).toBeInTheDocument()
      expect(previousPage()).toBeDisabled()

      await user.click(nextPage())

      expect(await card('Clinic 21')).toBeInTheDocument()
      expect(screen.queryByRole('link', { name: 'Clinic 01' })).not.toBeInTheDocument()
      expect(position()).toHaveTextContent('Showing 21–40 of 45 hospitals')
      expect(within(pager()).getByText('Page 2 of 3')).toBeInTheDocument()
      expect(lastListRequest(api)?.params).toEqual({ page: 2, page_size: 20 })
      expect(router.state.location.search).toBe('?page=2')
      // The button that was pressed has been replaced: focus is at the top of the new results.
      expect(screen.getByRole('heading', { name: 'Hospitals' })).toHaveFocus()

      await user.click(nextPage())

      // The last page is short, and says so.
      expect(await card('Clinic 45')).toBeInTheDocument()
      expect(cards()).toHaveLength(5)
      expect(position()).toHaveTextContent('Showing 41–45 of 45 hospitals')
      expect(within(pager()).getByText('Page 3 of 3')).toBeInTheDocument()
      expect(nextPage()).toBeDisabled()
      expect(lastListRequest(api)?.params).toEqual({ page: 3, page_size: 20 })

      // Each page is its own history entry: back is the page before.
      await act(() => router.navigate(-1))
      expect(await card('Clinic 21')).toBeInTheDocument()
      expect(router.state.location.search).toBe('?page=2')

      await user.click(previousPage())
      expect(await card('Clinic 01')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
    })

    it('keeps the search and the city while turning pages', async () => {
      const api = serve(hospitalDirectory(manyHospitals(45)))
      const { user, router } = open('/hospitals?q=clinic&city=Bengaluru')
      await card('Clinic 01')

      await user.click(nextPage())

      expect(await card('Clinic 21')).toBeInTheDocument()
      expect(lastListRequest(api)?.params).toEqual({ page: 2, page_size: 20, search: 'clinic', city: 'Bengaluru' })
      expect(router.state.location.search).toBe('?q=clinic&city=Bengaluru&page=2')
    })

    it('reports the position in the server’s numbers, not its own assumptions', async () => {
      // The server answers with a page size the app did not ask for.
      serve({ [LIST]: okPage(manyHospitals(10), 2, 10, 45), [CITIES]: ok({ cities: [] }) })
      open('/hospitals?page=2')

      expect(await card('Clinic 01')).toBeInTheDocument()
      expect(position()).toHaveTextContent('Showing 11–20 of 45 hospitals')
      expect(within(pager()).getByText('Page 2 of 5')).toBeInTheDocument()
    })

    it('BEYOND THE END — says the page is empty, how many there are, and leads back to the first', async () => {
      const api = serve(hospitalDirectory(manyHospitals(45)))
      const { user, router } = open('/hospitals?page=9')

      expect(await screen.findByText('There is nothing on this page')).toBeInTheDocument()
      expect(screen.getByText('There are 45 hospitals in all, on earlier pages.')).toBeInTheDocument()
      expect(position()).toHaveTextContent('Nothing on this page')
      expect(api.calls(LIST)[0].params).toEqual({ page: 9, page_size: 20 })
      // Not "no hospitals", and no "Page 9 of 3".
      expect(screen.queryByText('No hospitals to show yet')).not.toBeInTheDocument()
      expect(screen.queryByRole('navigation', { name: 'Pages of hospitals' })).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Go to the first page' }))

      expect(await card('Clinic 01')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
    })
  })

  describe('when it cannot load', () => {
    afterEach(() => onlineManager.setOnline(true))

    it('ERROR — shows the app’s own message and a retry that works', async () => {
      const api = serve({ ...hospitalDirectory(THREE), [LIST]: fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)') })
      const { user } = open()

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('We could not load the hospitals. Please try again.')
      expect(document.body).not.toHaveTextContent('Traceback')
      expect(screen.queryByText('No hospitals to show yet')).not.toBeInTheDocument()
      expect(position()).toBeEmptyDOMElement()

      api.on(hospitalDirectory(THREE))
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(2)
    })

    it.each([
      ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot show this in the app right now\. Please try again later\.$/],
      ['the rate limit (429)', fail(429, 'RATE_LIMITED', 'server wording'), /^Too many requests\. Please wait a moment and try again\.$/],
      ['a refused parameter (422)', fail(422, 'VALIDATION_ERROR', 'server wording'), /^We could not load the hospitals\. Please try again\.$/],
      ['an unknown refusal (403)', fail(403, 'FORBIDDEN', 'server wording'), /^We could not load the hospitals\. Please try again\.$/],
    ])('shows a safe message for %s', async (_case, outcome, message) => {
      serve({ ...hospitalDirectory(THREE), [LIST]: outcome })
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent('server wording')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    })

    it('OFFLINE — a request that gets no answer is called a connection problem, and can be retried', async () => {
      const api = serve({ ...hospitalDirectory(THREE), [LIST]: unreachable })
      const { user } = open()

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('No connection')
      expect(alert).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
      expect(screen.queryByText(/We could not load the hospitals/)).not.toBeInTheDocument()

      api.on(hospitalDirectory(THREE))
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it('OFFLINE — when the browser says it has no network, that is what the patient is told', async () => {
      vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false)
      serve({
        ...hospitalDirectory(THREE),
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
      const api = serve(hospitalDirectory(THREE))
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
      expect(screen.queryByText('Loading hospitals…')).not.toBeInTheDocument()
      expect(api.sent).toHaveLength(0)

      act(() => onlineManager.setOnline(true))

      expect(await card('City Care Hospital')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })
  })
})
