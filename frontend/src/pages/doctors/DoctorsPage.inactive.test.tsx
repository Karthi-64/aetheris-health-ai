import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { DoctorListParams, DoctorSummary } from '@/api/doctors'
import type { DepartmentSummary } from '@/api/departments'
import { signIn, signOut } from '@/test/auth'
import { fail, installFakeApi, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import DoctorsPage from './DoctorsPage'

/**
 * Finding a deactivated doctor in the directory. The API leaves them out of
 * `GET /doctors` unless `include_inactive=true` is sent, so the directory has
 * to be able to ask — but only for someone who can do something about one.
 *
 * The real hooks and Axios instance run against an in-memory server that
 * filters the way the API does, so the rows shown are the ones the request
 * actually asked for.
 */

// As seeded (backend/app/seeds/seed.py): only a Hospital Admin can change a doctor.
const ADMIN = ['doctor.read', 'doctor.update', 'doctor.delete', 'department.read']
const DOCTOR = ['doctor.read', 'department.read']

const DEPARTMENTS: DepartmentSummary[] = [
  { id: 'dep-cardio', code: 'CARD', name: 'Cardiology', location: null, status: 'active' },
  { id: 'dep-neuro', code: 'NEUR', name: 'Neurology', location: null, status: 'active' },
]

const DOCTORS: DoctorSummary[] = [
  {
    id: 'doc-1',
    user_id: 'user-9',
    full_name: 'Priya Sharma',
    specialization: 'Cardiology',
    department_id: 'dep-cardio',
    department_name: 'Cardiology',
    consultation_fee: '950.00',
    status: 'active',
  },
  {
    id: 'doc-2',
    user_id: 'user-12',
    full_name: 'Arjun Rao',
    specialization: 'Neurology',
    department_id: 'dep-neuro',
    department_name: 'Neurology',
    consultation_fee: '800.00',
    status: 'inactive',
  },
  {
    id: 'doc-3',
    user_id: 'user-15',
    full_name: 'Meera Sharma',
    specialization: 'Cardiology',
    department_id: 'dep-cardio',
    department_name: 'Cardiology',
    consultation_fee: '700.00',
    status: 'inactive',
  },
]

let api: FakeApi
/** How many pages the directory says it has, whichever list is asked for. */
let totalPages: number

/** `GET /doctors` as the API filters it. */
function list(params: DoctorListParams) {
  const term = params.q?.toLowerCase()
  return DOCTORS.filter(
    (d) =>
      (params.include_inactive === true || d.status === 'active') &&
      (!params.department || d.department_id === params.department) &&
      (!term || d.full_name.toLowerCase().split(' ').some((part) => part.startsWith(term))),
  )
}

/** The matching doctors, answered as the page that was asked for. */
function directoryPage(params: DoctorListParams): Outcome {
  const items = list(params)
  const pagination = {
    page: params.page ?? 1,
    page_size: params.page_size ?? 25,
    total_records: items.length,
    total_pages: totalPages,
  }
  return { status: 200, data: { success: true, message: 'ok', data: items, metadata: { pagination } } }
}

beforeEach(() => {
  totalPages = 1
  api = installFakeApi((config) => {
    if (config.url === '/doctors') return directoryPage(config.params ?? {})
    if (config.url === '/departments') return paged(DEPARTMENTS)
    return fail(404, 'Not Found', { error_code: 'RESOURCE_NOT_FOUND', errors: null })
  })
})

afterEach(() => {
  // Unmount before signing out, so no mounted component reacts to it.
  cleanup()
  api.restore()
  signOut()
})

function renderDirectory(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <DoctorsPage />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const listRequests = () => api.sent.filter((c) => c.method === 'get' && c.url === '/doctors')
const lastList = () => listRequests().at(-1)
const includeInactive = () => screen.getByRole('checkbox', { name: 'Include inactive' })
const row = (name: string) => screen.findByRole('row', { name: new RegExp(name) })

describe('inactive doctors in the directory', () => {
  it('leaves deactivated doctors out until asked', async () => {
    renderDirectory(ADMIN)

    expect(await row('Priya Sharma')).toBeInTheDocument()
    expect(screen.queryByText('Arjun Rao')).not.toBeInTheDocument()
    expect(includeInactive()).not.toBeChecked()

    const [request] = listRequests()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/doctors')
    expect(request.params).toEqual({ page: 1, page_size: 25 })
  })

  it('sends include_inactive=true when the control is on, and shows active doctors only when off', async () => {
    const user = userEvent.setup()
    renderDirectory(ADMIN)
    await row('Priya Sharma')

    await user.click(includeInactive())

    // Found, marked, and one click from the page it is reactivated on.
    const found = await row('Arjun Rao')
    expect(includeInactive()).toBeChecked()
    expect(lastList()?.params).toEqual({ include_inactive: true, page: 1, page_size: 25 })
    expect(within(found).getByText('inactive')).toBeInTheDocument()
    expect(within(found).getByRole('link', { name: 'View Arjun Rao' })).toHaveAttribute(
      'href',
      '/doctors/doc-2',
    )
    expect(within(await row('Priya Sharma')).getByText('active')).toBeInTheDocument()

    await user.click(includeInactive())

    await waitFor(() => expect(screen.queryByText('Arjun Rao')).not.toBeInTheDocument())
    expect(includeInactive()).not.toBeChecked()
    expect(await row('Priya Sharma')).toBeInTheDocument()
    // Back to the list first asked for, which is still fresh: one request without the flag, one with.
    expect(listRequests().map((c) => c.params.include_inactive)).toEqual([undefined, true])
  })

  it('keeps the search and the department filter working alongside it', async () => {
    const user = userEvent.setup()
    renderDirectory(ADMIN)
    await row('Priya Sharma')

    await user.click(includeInactive())
    await row('Arjun Rao')
    await user.type(screen.getByRole('textbox', { name: 'Search doctors' }), 'sharma')

    await waitFor(() =>
      expect(lastList()?.params).toEqual({ q: 'sharma', include_inactive: true, page: 1, page_size: 25 }),
    )
    expect(await row('Meera Sharma')).toBeInTheDocument()
    expect(await row('Priya Sharma')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Arjun Rao')).not.toBeInTheDocument())

    await user.clear(screen.getByRole('textbox', { name: 'Search doctors' }))
    await user.click(screen.getByRole('combobox', { name: 'Filter by department' }))
    await user.click(await screen.findByRole('option', { name: 'Neurology' }))

    await waitFor(() =>
      expect(lastList()?.params).toEqual({
        department: 'dep-neuro',
        include_inactive: true,
        page: 1,
        page_size: 25,
      }),
    )
    expect(await row('Arjun Rao')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText('Priya Sharma')).not.toBeInTheDocument())

    // Turning it off narrows the same filtered list back to active doctors.
    await user.click(includeInactive())
    await waitFor(() =>
      expect(lastList()?.params).toEqual({ department: 'dep-neuro', page: 1, page_size: 25 }),
    )
    expect(await screen.findByText('No matches')).toBeInTheDocument()
  })

  it('goes back to the first page when the control is switched, either way', async () => {
    totalPages = 3
    const user = userEvent.setup()
    renderDirectory(ADMIN)
    await row('Priya Sharma')

    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()
    expect(lastList()?.params).toEqual({ page: 2, page_size: 25 })

    // The list with inactive doctors is another list: its page 2 need not exist.
    await user.click(includeInactive())
    await row('Arjun Rao')
    expect(lastList()?.params).toEqual({ include_inactive: true, page: 1, page_size: 25 })
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('Page 2 of 3')).toBeInTheDocument()
    expect(lastList()?.params).toEqual({ include_inactive: true, page: 2, page_size: 25 })

    // And the same on the way back. Both pages of that list are still fresh,
    // so nothing is requested: the page shown is the evidence.
    await user.click(includeInactive())
    await waitFor(() => expect(screen.queryByText('Arjun Rao')).not.toBeInTheDocument())
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
    expect(listRequests()).toHaveLength(4)
  })

  it.each([
    ['only update', ['doctor.read', 'doctor.update']],
    ['only deactivate', ['doctor.read', 'doctor.delete']],
  ])('offers the control to a user who can %s doctors', async (_what, permissions) => {
    renderDirectory(permissions)
    await row('Priya Sharma')
    expect(includeInactive()).toBeInTheDocument()
  })

  it('does not offer the control to a read-only user, and never asks for inactive doctors', async () => {
    renderDirectory(DOCTOR)

    expect(await row('Priya Sharma')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Include inactive' })).not.toBeInTheDocument()
    expect(screen.queryByText('Include inactive')).not.toBeInTheDocument()
    expect(screen.queryByText('Arjun Rao')).not.toBeInTheDocument()
    expect(listRequests().every((c) => c.params.include_inactive === undefined)).toBe(true)
  })
})
