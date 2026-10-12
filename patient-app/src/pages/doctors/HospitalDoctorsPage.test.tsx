import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { onlineManager } from '@tanstack/react-query'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { DOCTOR_SEARCH_DEBOUNCE_MS } from '@/pages/doctors/HospitalDoctorsPage'
import { doctorDirectory } from '@/test/doctorDirectory'
import { deferred, fail, headerOf, ok, okPage, serve, unreachable, type Handler, type Outcome, type Routes } from '@/test/fakeApi'
import {
  ashaRao,
  cardiology,
  cityCareHospital,
  doctor,
  doctorRef,
  manyDoctors,
  meeraIyer,
  orthopaedics,
  vikramShah,
} from '@/test/fixtures'
import { isSignedIn, renderApp, signIn } from '@/test/renderApp'

const HOSPITAL = 'GET /hospitals/city-care'
const LIST = `${HOSPITAL}/doctors`
const DEPARTMENTS = `${HOSPITAL}/departments`
const REFRESH = 'POST /auth/refresh'
const FIRST_PAGE = { page: 1, page_size: 20 }
const PATH = '/hospitals/city-care/doctors'

/** Asha Rao (Cardiology, every field), Meera Iyer (Orthopaedics), Vikram Shah (nothing optional). */
const THREE = [ashaRao, meeraIyer, vikramShah]

/** City Care Hospital and its doctor endpoints over `doctors`. */
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
const searchField = () => screen.findByRole('searchbox', { name: 'Search by name or specialisation' })
const departmentFilter = () => screen.getByRole('combobox', { name: 'Department' })
const departmentOptions = () => within(departmentFilter()).getAllByRole('option').map((option) => option.textContent)
/** A doctor's card, by the name on it. */
const card = async (name: string | RegExp) => (await screen.findByRole('heading', { level: 3, name })).closest('li') as HTMLElement
const cards = () => within(screen.getByRole('list', { name: 'Doctors' })).getAllByRole('listitem')
const names = () => within(screen.getByRole('list', { name: 'Doctors' })).getAllByRole('heading', { level: 3 }).map((each) => each.textContent)
/** The live line under "Doctors": the count, or where the patient is in it. */
const position = () => screen.getByRole('status')
const lastListRequest = (api: ReturnType<typeof serve>) => api.calls(LIST).at(-1)

// The search delay is stepped through with the clock stopped, as in `HospitalsPage.test.tsx`.
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

const UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i

describe('doctor discovery — a hospital’s doctors', () => {
  it('shows skeletons while the first page loads, then the doctors', async () => {
    const { handler, answer } = deferred()
    serve({ ...cityCareWith(), [LIST]: handler })
    open()

    expect(await screen.findByText('Loading doctors…')).toHaveAttribute('role', 'status')
    expect(await title('Doctors at City Care Hospital')).toBeInTheDocument()
    expect(main().querySelectorAll('[data-slot="skeleton"]').length).toBeGreaterThan(0)
    expect(screen.queryByRole('list', { name: 'Doctors' })).not.toBeInTheDocument()

    answer(okPage(THREE))

    expect(await card('Asha Rao')).toBeInTheDocument()
    expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
    expect(screen.queryByText('Loading doctors…')).not.toBeInTheDocument()
  })

  it('lists what the server returns, in its order, asking only for the hospital, a page and the departments', async () => {
    const api = serve(cityCareWith())
    open()
    await card('Asha Rao')

    expect(names()).toEqual(['Asha Rao', 'Meera Iyer', 'Vikram Shah'])
    expect(position()).toHaveTextContent(/^3 doctors$/)
    // Everything fits on one page: no page controls to mislead.
    expect(screen.queryByRole('navigation', { name: 'Pages of doctors' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to City Care Hospital' })).toHaveAttribute('href', '/hospitals/city-care')
    expect(document.title).toBe('Doctors · Atheris Health')

    // The requests name the hospital by its public reference and nothing else — the token says who is asking.
    expect(api.sent.map((request) => `${request.method?.toUpperCase()} ${request.url}`).sort()).toEqual(
      [HOSPITAL, DEPARTMENTS, LIST].sort(),
    )
    const [request] = api.calls(LIST)
    expect(request.params).toEqual(FIRST_PAGE)
    expect(headerOf(request, 'Authorization')).toBe('Bearer access-1')
    expect(api.calls(DEPARTMENTS)[0].params).toBeUndefined()
    expect(headerOf(api.calls(DEPARTMENTS)[0], 'Authorization')).toBe('Bearer access-1')
  })

  it('shows on a card the name, specialisation, department, degrees and languages, and a way to the profile', async () => {
    serve(cityCareWith())
    open()

    const asha = await card('Asha Rao')
    expect(within(asha).getByText('Interventional Cardiology')).toBeInTheDocument()
    expect(within(asha).getByText('Cardiology')).toBeInTheDocument()
    expect(within(asha).getByText('Qualifications:').closest('div')).toHaveTextContent('Qualifications: MBBS, MD')
    expect(within(asha).getByText('Languages:').closest('div')).toHaveTextContent('Languages: English, Kannada')
    // The name is as the hospital keeps it: no title is put in front of it.
    expect(asha).not.toHaveTextContent(/\bDr\b/)
    // The link says whose profile it is; the reference is in the address only.
    const profile = within(asha).getByRole('link', { name: 'View Profile Asha Rao' })
    expect(profile).toHaveTextContent(/^View Profile$/)
    expect(profile).toHaveAttribute('href', `/hospitals/city-care/doctors/${ashaRao.ref}`)
    // A card carries no bio, institution or year: those are for the profile.
    expect(asha).not.toHaveTextContent(/Looks after|Bangalore Medical College|2008/)

    // Nothing optional: no empty label, no blank line, nothing invented.
    const vikram = await card('Vikram Shah')
    expect(vikram).toHaveTextContent(/^Vikram ShahGeneral MedicineView Profile$/)
    expect(within(vikram).queryByText(/Qualifications|Languages/)).not.toBeInTheDocument()
    expect(main()).not.toHaveTextContent(/null|undefined/)
    expect(within(main()).queryByRole('img')).not.toBeInTheDocument()
  })

  it('NEVER SHOWS A REFERENCE — no doctor, department or hospital id in the text, only in the links that need one', async () => {
    serve(cityCareWith())
    open()
    await card('Asha Rao')
    await waitFor(() => expect(departmentOptions()).toHaveLength(3))

    expect(document.body.textContent).not.toMatch(UUID)
    for (const element of document.querySelectorAll('*')) {
      for (const attribute of element.attributes) {
        // A link to a profile and the value a filter option sends are the only places a reference may be.
        if (attribute.name === 'href' || (element.tagName === 'OPTION' && attribute.name === 'value')) continue
        expect(attribute.value).not.toMatch(UUID)
      }
    }
  })

  it('ATTACK — a response carrying more than the contract allows: only the allow-listed fields reach the screen', async () => {
    const leaky = {
      ...ashaRao,
      id: '5f0c2a9e-7b1d-4c58-9e7a-2f6d1b3c4a55',
      user_id: '7a7a7a7a-1111-4222-8333-444444444444',
      hospital_id: '9b9b9b9b-1111-4222-8333-444444444444',
      license_number: 'KMC-SECRET-77',
      email: 'asha@secret.example',
      phone: '+91 99999 00000',
      rating: 4.8,
      review_count: 321,
      reviews: [{ text: 'SECRET-REVIEW' }],
      consultation_fee: '750.00',
      fee: 750,
      experience_years: 17,
      years_of_experience: 17,
      slots: [{ start: '2026-10-08T09:30:00Z' }],
      next_available: '2026-10-08T09:30:00Z',
      available_today: true,
      distance_km: 4.2,
      photo_url: 'https://cdn.example.test/doctors/asha.png',
      is_active: true,
      sponsored: true,
      listing: 'promoted',
      department: { ...ashaRao.department, id: 'DEPT-SECRET', head_doctor_id: 'HEAD-SECRET' },
      qualifications: [{ degree: 'MBBS', institution: null, year: null, certificate_url: 'https://cdn.example.test/cert.pdf' }],
    }
    serve({ ...cityCareWith(), [LIST]: okPage([leaky]) })
    open()
    const asha = await card('Asha Rao')

    expect(asha).toHaveTextContent(/^Asha RaoInterventional CardiologyCardiologyQualifications: MBBSLanguages: English, KannadaView Profile$/)
    const page = document.body.innerHTML
    for (const secret of ['5f0c2a9e', '7a7a7a7a', '9b9b9b9b', 'KMC-SECRET', 'secret.example', '99999', 'SECRET-REVIEW', '750.00', '2026-10-08', '09:30', 'cdn.example.test', 'DEPT-SECRET', 'HEAD-SECRET']) {
      expect(page).not.toContain(secret)
    }
    // The rating, the review count, the fee and the years are numbers: the only one on the page is the count, "1 doctor".
    expect(main().textContent?.match(/\d+/g)).toEqual(['1'])
    expect(main()).not.toHaveTextContent(/rating|review|\bfee\b|₹|experience|available|slot|\bkm\b|sponsored|promoted|featured|email|phone|licen[cs]e/i)
    expect(document.querySelector('main img, main [src], main [style]')).toBeNull()
  })

  it('ATTACK — markup in a name, specialisation, department, degree or language is shown as the characters it is', async () => {
    const hostile = doctor({
      name: '<img src=x onerror="window.pwned=1">Asha',
      specialization: '<script>window.pwned=1</script>Cardiology',
      department: { ref: cardiology.ref, name: '<b>Heart</b>' },
      qualifications: [{ degree: '<i>MBBS</i>', institution: '<u>College</u>', year: 2008 }],
      languages: ['<svg onload="window.pwned=1">English'],
    })
    serve({
      ...cityCareWith(),
      [LIST]: okPage([hostile]),
      [DEPARTMENTS]: ok({ departments: [{ ref: cardiology.ref, name: '"><script>window.pwned=1</script>', description: '<b>x</b>' }] }),
    })
    open()

    const evil = await card('<img src=x onerror="window.pwned=1">Asha')
    for (const literal of ['<script>window.pwned=1</script>Cardiology', '<b>Heart</b>', '<i>MBBS</i>', '<svg onload="window.pwned=1">English']) {
      expect(within(evil).getByText(literal)).toBeInTheDocument()
    }
    await waitFor(() => expect(departmentOptions()).toEqual(['All departments', '"><script>window.pwned=1</script>']))
    expect(main().querySelector('script, img, b, i, u, svg[onload], [onerror], [onclick]')).toBeNull()
    expect((window as { pwned?: unknown }).pwned).toBeUndefined()
  })

  it('is labelled for assistive technology: a search landmark, named fields, a live count', async () => {
    serve(cityCareWith())
    open()
    await card('Asha Rao')
    await waitFor(() => expect(departmentOptions()).toHaveLength(3))

    const form = screen.getByRole('search', { name: 'Find a doctor' })
    const field = within(form).getByRole('searchbox', { name: 'Search by name or specialisation' })
    expect(field).toHaveAttribute('type', 'search')
    expect(field).toHaveAttribute('maxlength', '80')
    expect(within(form).getByRole('combobox', { name: 'Department' })).toBeInTheDocument()

    expect(position()).toHaveTextContent('3 doctors')
    expect(screen.getByRole('region', { name: 'Doctors' })).toContainElement(position())
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    // Still under "Hospitals" in the navigation.
    expect(screen.getByRole('link', { name: 'Hospitals' })).toHaveAttribute('aria-current', 'page')

    const touchSized = /(^|\s)(h-11|min-h-11|size-11)(\s|$)/
    const controls = [...main().querySelectorAll('a, button, input, select')]
    // Back, the field, the department filter, three profile links.
    expect(controls).toHaveLength(6)
    for (const control of controls) expect(control.className).toMatch(touchSized)
    expect(within(main()).queryByRole('table')).not.toBeInTheDocument()
  })

  it('NOT FOUND — an unknown or unavailable hospital has no doctors page, and nothing is asked about its doctors', async () => {
    const api = serve({ [HOSPITAL]: fail(404, 'RESOURCE_NOT_FOUND', 'server wording that must not be shown') })
    open()

    expect(await title('This hospital is not available')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Browse hospitals' })).toHaveAttribute('href', '/hospitals')
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('server wording')
    expect(api.sent).toHaveLength(1)
  })

  it.each([
    ['a path step', '/hospitals/..%2F..%2Fme/doctors'],
    ['a dot', '/hospitals/./doctors'.replace('/./', '/%2E/')],
    ['a query in the segment', '/hospitals/city-care%3Fx=1/doctors'],
    ['more than a reference holds', `/hospitals/${'a'.repeat(101)}/doctors`],
  ])('ATTACK — a hospital reference that is %s is never sent, for the hospital or for its doctors', async (_case, path) => {
    const api = serve({})
    open(path)

    expect(await title('This hospital is not available')).toBeInTheDocument()
    expect(api.sent).toHaveLength(0)
  })

  describe('search', () => {
    it('waits for typing to pause, then asks once with the final text and puts the search in the URL', async () => {
      const api = serve(cityCareWith())
      const { router } = open()
      const field = await searchField()
      await card('Asha Rao')
      field.focus()
      vi.useFakeTimers()

      // Each keystroke starts the wait again: more than the delay passes in all, and nothing is asked.
      for (const soFar of ['j', 'jo', 'joi', 'join', 'joint']) {
        typed(field, soFar)
        await pass(DOCTOR_SEARCH_DEBOUNCE_MS - 1)
      }
      expect(api.calls(LIST)).toHaveLength(1)
      expect(router.state.location.search).toBe('')

      await pass(1)

      // One request for the whole word — here it matches a specialisation, not a name.
      await until(() => expect(api.calls(LIST)).toHaveLength(2))
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, search: 'joint' })
      expect(router.state.location.search).toBe('?q=joint')
      await until(() => expect(names()).toEqual(['Meera Iyer']))
      expect(position()).toHaveTextContent(/^1 doctor$/)
      // The patient is still typing: the field keeps its text and the focus.
      expect(field).toHaveValue('joint')
      expect(field).toHaveFocus()

      await pass(DOCTOR_SEARCH_DEBOUNCE_MS * 3)
      expect(api.calls(LIST)).toHaveLength(2)
      // Typing asked for nothing else: the hospital and the departments were read once.
      expect(api.calls(HOSPITAL)).toHaveLength(1)
      expect(api.calls(DEPARTMENTS)).toHaveLength(1)
    })

    it('searches at once on Enter, trimmed, and from a later page goes back to the first', async () => {
      const api = serve(cityCareWith(manyDoctors(45)))
      const { router } = open(`${PATH}?page=2`)
      const field = await searchField()
      await card('Doctor 21')
      vi.useFakeTimers()

      typed(field, '  doctor 4  ')
      fireEvent.submit(screen.getByRole('search'))

      await until(() => expect(api.calls(LIST)).toHaveLength(2))
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, search: 'doctor 4' })
      expect(router.state.location.search).toBe('?q=doctor+4')
      await pass(DOCTOR_SEARCH_DEBOUNCE_MS * 3)
      expect(api.calls(LIST)).toHaveLength(2)
      expect(position()).toHaveTextContent(/^6 doctors$/)
    })

    it('RELOAD — the search, the department and the page all come back from the URL', async () => {
      const api = serve(cityCareWith(manyDoctors(45)))
      open(`${PATH}?q=doctor&dept=${cardiology.ref}&page=2`)

      expect(await card('Doctor 21')).toBeInTheDocument()
      expect(await searchField()).toHaveValue('doctor')
      await waitFor(() => expect(departmentFilter()).toHaveDisplayValue('Cardiology'))
      expect(position()).toHaveTextContent('Showing 21–40 of 45 doctors')
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual({ page: 2, page_size: 20, search: 'doctor', department: cardiology.ref })
    })

    it('clears with one press, keeping the field in hand', async () => {
      serve(cityCareWith())
      const { user, router } = open(`${PATH}?q=rao`)
      const field = await searchField()
      expect(field).toHaveValue('rao')
      await card('Asha Rao')
      expect(names()).toEqual(['Asha Rao'])

      await user.click(screen.getByRole('button', { name: 'Clear search' }))

      expect(await card('Vikram Shah')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(field).toHaveValue('')
      expect(field).toHaveFocus()
    })

    it('BACK — the field follows the URL, and the list with it', async () => {
      serve(cityCareWith())
      const { user, router } = open()
      const field = await searchField()
      await card('Asha Rao')

      await user.type(field, 'shah{Enter}')
      await waitFor(() => expect(names()).toEqual(['Vikram Shah']))
      await user.click(screen.getByRole('link', { name: 'View Profile Vikram Shah' }))
      await title('Vikram Shah')

      await act(() => router.navigate(-1))

      expect(await searchField()).toHaveValue('shah')
      expect(router.state.location.search).toBe('?q=shah')
      await waitFor(() => expect(names()).toEqual(['Vikram Shah']))
    })

    it('ATTACK — over-long text, a bad page and filters the server does not offer in the URL: cut, defaulted and not forwarded', async () => {
      const api = serve(cityCareWith())
      open(`${PATH}?q=${'a'.repeat(500)}&page=1;DROP TABLE doctors&include_hidden=1&hospital_id=5f0c2a9e&page_size=5000&search=x&sort=id`)

      expect(await screen.findByText('No doctors match your search')).toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: 'a'.repeat(80) })
    })

    it('sends wildcard characters as the text they are', async () => {
      const api = serve(cityCareWith())
      open(`${PATH}?q=${encodeURIComponent('100%_\\')}`)

      expect(await screen.findByText('No doctors match your search')).toBeInTheDocument()
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: '100%_\\' })
    })
  })

  describe('department filter', () => {
    it('offers the departments the server lists, by name, and filters by the reference of the one chosen', async () => {
      const api = serve(cityCareWith())
      const { user, router } = open()
      await card('Asha Rao')
      await waitFor(() => expect(departmentOptions()).toEqual(['All departments', 'Cardiology', 'Orthopaedics']))
      expect(departmentFilter()).toHaveValue('')

      await user.selectOptions(departmentFilter(), 'Orthopaedics')

      await waitFor(() => expect(names()).toEqual(['Meera Iyer']))
      expect(position()).toHaveTextContent(/^1 doctor$/)
      expect(lastListRequest(api)?.params).toEqual({ ...FIRST_PAGE, department: orthopaedics.ref })
      expect(router.state.location.search).toBe(`?dept=${orthopaedics.ref}`)

      await user.selectOptions(departmentFilter(), 'All departments')

      await waitFor(() => expect(names()).toHaveLength(3))
      expect(router.state.location.search).toBe('')
      // The full list was read a moment ago: it is shown again without asking twice.
      expect(api.calls(LIST)).toHaveLength(2)
    })

    it('combines with the search', async () => {
      const api = serve(cityCareWith())
      open(`${PATH}?q=a&dept=${cardiology.ref}`)

      await card('Asha Rao')
      expect(names()).toEqual(['Asha Rao'])
      expect(api.calls(LIST)[0].params).toEqual({ ...FIRST_PAGE, search: 'a', department: cardiology.ref })
    })

    it('is not offered when the hospital has no departments to offer', async () => {
      serve(cityCareWith([vikramShah]))
      open()

      await card('Vikram Shah')
      await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument())
      expect(screen.queryByText('Department')).not.toBeInTheDocument()
    })

    it.each([
      ['fails', fail(500, 'INTERNAL_ERROR', 'Traceback…')],
      ['cannot be reached', unreachable],
      ['is not a list at all', ok({ departments: 'Cardiology' })],
      ['has no body', ok(null)],
    ])('still lists the doctors when the departments request %s — just without the filter', async (_case, answer) => {
      serve({ ...cityCareWith(), [DEPARTMENTS]: answer })
      open()

      await card('Asha Rao')
      expect(names()).toHaveLength(3)
      expect(position()).toHaveTextContent(/^3 doctors$/)
      await waitFor(() => expect(screen.queryByRole('combobox')).not.toBeInTheDocument())
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(document.body).not.toHaveTextContent('Traceback')
    })

    it('offers only departments it could send and name, whatever else the response carries', async () => {
      const junk = [cardiology, { ref: 'not-a-reference', name: 'Radiology' }, { ref: orthopaedics.ref, name: '   ' }, { name: 'Dermatology' }, 42, null, orthopaedics]
      serve({ ...cityCareWith(), [DEPARTMENTS]: ok({ departments: junk }) })
      open()
      await card('Asha Rao')

      await waitFor(() => expect(departmentOptions()).toEqual(['All departments', 'Cardiology', 'Orthopaedics']))
    })

    it('keeps a department in force visible and clearable when the departments cannot be loaded — by a neutral label, never its reference', async () => {
      serve({ ...cityCareWith(), [DEPARTMENTS]: fail(500, 'INTERNAL_ERROR') })
      const { user, router } = open(`${PATH}?dept=${orthopaedics.ref}`)

      await card('Meera Iyer')
      await waitFor(() => expect(departmentOptions()).toEqual(['All departments', 'Selected department']))
      expect(departmentFilter()).toHaveDisplayValue('Selected department')
      expect(document.body.textContent).not.toMatch(UUID)

      await user.selectOptions(departmentFilter(), 'All departments')

      await waitFor(() => expect(names()).toHaveLength(3))
      expect(router.state.location.search).toBe('')
    })

    it.each([
      ['words', 'cardiology'],
      ['a path step', '../../me'],
      ['SQL', "1' OR '1'='1"],
      ['a reference with something after it', `${cardiology.ref}x`],
    ])('ATTACK — a department in the URL that is %s is never sent: it matches no doctor, and can be cleared', async (_case, value) => {
      const api = serve(cityCareWith())
      const { user, router } = open(`${PATH}?dept=${encodeURIComponent(value)}`)

      // Not "every doctor", as if there were no filter — and not a request the server would refuse.
      expect(await screen.findByText('No doctors match your search')).toBeInTheDocument()
      expect(position()).toHaveTextContent('No doctors match')
      expect(api.calls(LIST)).toHaveLength(0)
      expect(screen.queryByRole('list', { name: 'Doctors' })).not.toBeInTheDocument()
      expect(departmentFilter()).toHaveDisplayValue('Selected department')
      expect(main()).not.toHaveTextContent(value)

      await user.click(screen.getByRole('button', { name: 'Clear search and department' }))

      expect(await card('Asha Rao')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
      expect(api.calls(LIST)).toHaveLength(1)
      expect(api.calls(LIST)[0].params).toEqual(FIRST_PAGE)
    })
  })

  describe('empty states', () => {
    it('NONE AT ALL — says the hospital has no doctors listed, with nothing to clear', async () => {
      serve(cityCareWith([]))
      open()

      expect(await screen.findByText('No doctors listed yet')).toBeInTheDocument()
      expect(screen.getByText('This hospital has no doctors listed in the app right now. Please check again later.')).toBeInTheDocument()
      expect(position()).toHaveTextContent('No doctors to show')
      expect(screen.queryByText('No doctors match your search')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Clear search and department' })).not.toBeInTheDocument()
      expect(screen.queryByRole('list', { name: 'Doctors' })).not.toBeInTheDocument()
      // No doctor, count or placeholder card stands in for one.
      expect(main()).not.toHaveTextContent(/\d/)
      expect(main().querySelector('[data-slot="skeleton"]')).toBeNull()
    })

    it('NOTHING MATCHES — says so, differently, and clears back to every doctor', async () => {
      serve(cityCareWith())
      const { user, router } = open(`${PATH}?q=zzz&dept=${cardiology.ref}`)

      expect(await screen.findByText('No doctors match your search')).toBeInTheDocument()
      expect(position()).toHaveTextContent('No doctors match')
      expect(screen.queryByText('No doctors listed yet')).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Clear search and department' }))

      expect(await card('Asha Rao')).toBeInTheDocument()
      expect(cards()).toHaveLength(3)
      expect(router.state.location.search).toBe('')
      expect(await searchField()).toHaveValue('')
      expect(await searchField()).toHaveFocus()
      expect(departmentFilter()).toHaveValue('')
    })
  })

  describe('pagination', () => {
    const nextPage = () => screen.getByRole('button', { name: 'Next' })
    const previousPage = () => screen.getByRole('button', { name: 'Previous' })
    const pager = () => screen.getByRole('navigation', { name: 'Pages of doctors' })

    it('pages through the results, saying truthfully where the patient is', async () => {
      const api = serve(cityCareWith(manyDoctors(45)))
      const { user, router } = open()

      expect(await card('Doctor 01')).toBeInTheDocument()
      expect(cards()).toHaveLength(20)
      expect(position()).toHaveTextContent('Showing 1–20 of 45 doctors')
      expect(within(pager()).getByText('Page 1 of 3')).toBeInTheDocument()
      expect(previousPage()).toBeDisabled()

      await user.click(nextPage())

      expect(await card('Doctor 21')).toBeInTheDocument()
      expect(screen.queryByRole('heading', { name: 'Doctor 01' })).not.toBeInTheDocument()
      expect(position()).toHaveTextContent('Showing 21–40 of 45 doctors')
      expect(lastListRequest(api)?.params).toEqual({ page: 2, page_size: 20 })
      expect(router.state.location.search).toBe('?page=2')
      // The button that was pressed has been replaced: focus is at the top of the new results.
      expect(screen.getByRole('heading', { level: 2, name: 'Doctors' })).toHaveFocus()

      await user.click(nextPage())

      expect(await card('Doctor 45')).toBeInTheDocument()
      expect(cards()).toHaveLength(5)
      expect(position()).toHaveTextContent('Showing 41–45 of 45 doctors')
      expect(nextPage()).toBeDisabled()

      // Each page is its own history entry: back is the page before.
      await act(() => router.navigate(-1))
      expect(await card('Doctor 21')).toBeInTheDocument()
      expect(router.state.location.search).toBe('?page=2')

      await user.click(previousPage())
      expect(await card('Doctor 01')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
    })

    it('keeps the search and the department while turning pages', async () => {
      const api = serve(cityCareWith(manyDoctors(45)))
      const { user, router } = open(`${PATH}?q=doctor&dept=${cardiology.ref}`)
      await card('Doctor 01')

      await user.click(nextPage())

      expect(await card('Doctor 21')).toBeInTheDocument()
      expect(lastListRequest(api)?.params).toEqual({ page: 2, page_size: 20, search: 'doctor', department: cardiology.ref })
      expect(router.state.location.search).toBe(`?q=doctor&dept=${cardiology.ref}&page=2`)
    })

    it('BEYOND THE END — says the page is empty, how many there are, and leads back to the first', async () => {
      serve(cityCareWith(manyDoctors(45)))
      const { user, router } = open(`${PATH}?page=9`)

      expect(await screen.findByText('There is nothing on this page')).toBeInTheDocument()
      expect(screen.getByText('There are 45 doctors in all, on earlier pages.')).toBeInTheDocument()
      expect(position()).toHaveTextContent('Nothing on this page')
      expect(screen.queryByText('No doctors listed yet')).not.toBeInTheDocument()

      await user.click(screen.getByRole('button', { name: 'Go to the first page' }))

      expect(await card('Doctor 01')).toBeInTheDocument()
      expect(router.state.location.search).toBe('')
    })
  })

  describe('when it cannot load', () => {
    afterEach(() => onlineManager.setOnline(true))

    const GATEWAY_PAGE: Outcome = {
      status: 503,
      data: '<html><body><h1>503 Service Temporarily Unavailable</h1><script>window.pwned=1</script>nginx</body></html>',
    }

    it('ERROR — shows the app’s own message, keeps the hospital and the search in view, and a retry that works', async () => {
      const api = serve({ ...cityCareWith(), [LIST]: fail(500, 'INTERNAL_ERROR', 'Traceback (most recent call last)') })
      const { user } = open(`${PATH}?q=rao`)

      const alert = await screen.findByRole('alert')
      expect(alert).toHaveTextContent('We could not load the doctors. Please try again.')
      expect(document.body).not.toHaveTextContent('Traceback')
      // A failure is never dressed up as an empty result.
      expect(document.body).not.toHaveTextContent(/No doctors|nothing on this page|0 doctors/i)
      expect(position()).toBeEmptyDOMElement()
      expect(screen.queryByRole('list', { name: 'Doctors' })).not.toBeInTheDocument()
      expect(screen.getByRole('heading', { level: 1, name: 'Doctors at City Care Hospital' })).toBeInTheDocument()
      expect(await searchField()).toHaveValue('rao')

      api.on(cityCareWith())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await card('Asha Rao')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(2)
      // Focus is at the top of the results, not lost with the button that was pressed.
      expect(screen.getByRole('heading', { level: 2, name: 'Doctors' })).toHaveFocus()
    })

    it('OFFLINE and 503 are told apart, in the app’s words, and each Retry asks exactly once more', async () => {
      const api = serve({ ...cityCareWith(), [LIST]: GATEWAY_PAGE })
      const { user } = open()

      const message = 'We could not load the doctors. Please try again.'
      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent(/No connection|internet connection/)
      expect(document.body).not.toHaveTextContent(/503|Service Temporarily|nginx|Network Error|Request failed/)
      expect((window as { pwned?: unknown }).pwned).toBeUndefined()

      // The connection drops: now, and only now, the app says so.
      api.on({ [LIST]: unreachable })
      await user.click(screen.getByRole('button', { name: 'Try again' }))
      await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('No connection'))
      expect(screen.getByRole('alert')).toHaveTextContent('We could not reach the server. Check your internet connection and try again.')
      expect(screen.queryByText(message)).not.toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(2)

      api.on(cityCareWith())
      await waitFor(() => expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await card('Asha Rao')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(api.calls(LIST)).toHaveLength(3)
    })

    it('OFFLINE — a request held back for lack of a network is not shown as loading for ever, and resumes by itself', async () => {
      serve(cityCareWith())
      const { router } = open('/hospitals/city-care')
      await title('City Care Hospital')

      onlineManager.setOnline(false)
      await act(() => router.navigate(PATH))

      expect(await screen.findByRole('alert')).toHaveTextContent('No connection')
      expect(screen.queryByText('Loading doctors…')).not.toBeInTheDocument()

      act(() => onlineManager.setOnline(true))

      expect(await card('Asha Rao')).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    })

    it.each([
      ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot show this in the app right now\. Please try again later\.$/],
      ['the rate limit (429)', fail(429, 'RATE_LIMITED', 'server wording'), /^Too many requests\. Please wait a moment and try again\.$/],
      ['a refused parameter (422)', fail(422, 'VALIDATION_ERROR', 'server wording'), /^We could not load the doctors\. Please try again\.$/],
      // The hospital was just read: a 404 for its list is a list that did not load, not a missing hospital.
      ['a 404 for the list alone', fail(404, 'RESOURCE_NOT_FOUND', 'server wording'), /^We could not load the doctors\. Please try again\.$/],
    ])('shows a safe message for %s', async (_case, outcome, message) => {
      serve({ ...cityCareWith(), [LIST]: outcome })
      open()

      expect(await screen.findByRole('alert')).toHaveTextContent(message)
      expect(document.body).not.toHaveTextContent('server wording')
      expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled()
    })

    it.each([
      ['without its pagination', { success: true, message: 'ok', data: THREE, metadata: { request_id: 'r' } }],
      ['whose data is not a list', { success: true, message: 'ok', data: { doctors: THREE }, metadata: { pagination: { page: 1, page_size: 20, total_records: 3, total_pages: 1 } } }],
      ['whose data is missing', { success: true, message: 'ok', data: null, metadata: { pagination: { page: 1, page_size: 20, total_records: 3, total_pages: 1 } } }],
      ['with a doctor that has no reference', { success: true, message: 'ok', data: [ashaRao, { ...meeraIyer, ref: undefined }], metadata: { pagination: { page: 1, page_size: 20, total_records: 2, total_pages: 1 } } }],
      ['with a doctor whose reference is not one', { success: true, message: 'ok', data: [{ ...ashaRao, ref: '../../me' }], metadata: { pagination: { page: 1, page_size: 20, total_records: 1, total_pages: 1 } } }],
      ['with a doctor that has no name', { success: true, message: 'ok', data: [{ ...ashaRao, name: null }], metadata: { pagination: { page: 1, page_size: 20, total_records: 1, total_pages: 1 } } }],
      ['with something that is not a doctor', { success: true, message: 'ok', data: ['Asha Rao'], metadata: { pagination: { page: 1, page_size: 20, total_records: 1, total_pages: 1 } } }],
    ])('a 200 %s is a failure with a retry, not a broken page and not an invented count', async (_case, body) => {
      const api = serve({ ...cityCareWith(), [LIST]: { status: 200, data: body } })
      const { user } = open()

      expect(await screen.findByRole('alert')).toHaveTextContent('We could not load the doctors. Please try again.')
      expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
      expect(position()).toBeEmptyDOMElement()
      expect(screen.queryByRole('list', { name: 'Doctors' })).not.toBeInTheDocument()

      api.on(cityCareWith())
      await user.click(screen.getByRole('button', { name: 'Try again' }))

      expect(await card('Asha Rao')).toBeInTheDocument()
    })

    it('a doctor whose optional parts are malformed is still listed, without them', async () => {
      const ragged = { ref: doctorRef(9), name: 'Ragged Entry', specialization: 7, department: 'Cardiology', qualifications: [{ institution: 'No degree' }, 'MBBS', { degree: 'MD', institution: 5, year: '2010' }], languages: 'English', bio: {} }
      serve({ ...cityCareWith(), [LIST]: okPage([ragged]) })
      open()

      expect(await card('Ragged Entry')).toHaveTextContent(/^Ragged EntryQualifications: MDView Profile$/)
    })
  })

  describe('session', () => {
    /** Answers only the refreshed token; the old one is refused, as an expired token is. */
    const onlyFor = (token: string, answer: Outcome | Handler, seen: string[] = []): Handler => (config) => {
      seen.push(`${config.url} ${headerOf(config, 'Authorization')}`)
      if (headerOf(config, 'Authorization') !== `Bearer ${token}`) return fail(401, 'AUTHENTICATION_REQUIRED', 'server wording')
      return typeof answer === 'function' ? answer(config) : answer
    }

    it('an expired token on the doctors and the departments together: one refresh, both retried with the new token', async () => {
      signIn('access-old')
      const seen: string[] = []
      const directory = cityCareWith()
      const api = serve({
        ...directory,
        [LIST]: onlyFor('access-new', directory[LIST], seen),
        [DEPARTMENTS]: onlyFor('access-new', directory[DEPARTMENTS], seen),
        [REFRESH]: ok({ access_token: 'access-new', expires_in: 900 }),
      })
      // The hospital is already in hand: only the doctor calls meet the expired token.
      const { router } = renderApp('/hospitals/city-care')
      await title('City Care Hospital')
      await act(() => router.navigate(`${PATH}?q=rao`))

      expect(await card('Asha Rao')).toBeInTheDocument()
      await waitFor(() => expect(departmentOptions()).toHaveLength(3))

      expect(api.calls(REFRESH)).toHaveLength(1)
      expect(headerOf(api.calls(REFRESH)[0], 'Authorization')).toBeUndefined()
      expect([...seen].sort()).toEqual([
        '/hospitals/city-care/departments Bearer access-new',
        '/hospitals/city-care/departments Bearer access-old',
        '/hospitals/city-care/doctors Bearer access-new',
        '/hospitals/city-care/doctors Bearer access-old',
      ])
      // The retry is the same question, on the same page.
      expect(api.calls(LIST)[1].params).toEqual({ ...FIRST_PAGE, search: 'rao' })
      expect(router.state.location.pathname + router.state.location.search).toBe(`${PATH}?q=rao`)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      expect(document.body).not.toHaveTextContent('server wording')
      expect(isSignedIn()).toBe(true)
    })

    it('ATTACK — a session that dies on the doctors page: one refresh, one sign-out, no doctor left on screen', async () => {
      signIn('access-old')
      const api = serve({
        [HOSPITAL]: ok(cityCareHospital),
        [LIST]: fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'),
        [DEPARTMENTS]: fail(401, 'AUTHENTICATION_REQUIRED', 'server wording'),
        [REFRESH]: fail(401, 'UNAUTHORIZED'),
      })
      const { router } = renderApp(PATH)

      expect(await screen.findByText('You have been signed out. Please sign in again.')).toBeInTheDocument()
      expect(router.state.location.pathname).toBe('/login')
      expect(isSignedIn()).toBe(false)
      expect(api.calls(REFRESH)).toHaveLength(1)
      // Asked once with the dead token and never again: no retry loop.
      expect(api.calls(LIST)).toHaveLength(1)
      expect(document.body).not.toHaveTextContent(/City Care|Asha|server wording|could not load/)
    })
  })
})
