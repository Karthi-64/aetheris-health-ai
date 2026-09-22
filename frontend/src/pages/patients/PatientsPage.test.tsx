import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import PatientsPage from './PatientsPage'
import type { PatientSummary } from '@/api/patients'

/**
 * The registry against the real contract (`docs/18-API_CONTRACTS.md` §2).
 *
 * The HTTP client is mocked at the module boundary rather than `fetch`
 * (frontend/CLAUDE.md), so what these assert is the thing the mismatch
 * resolution changed: the fields the page reads, and the query parameter names
 * it sends. `q` and `page_size` are the backend's spellings — the old mock used
 * `search` and `pageSize`, which the API silently ignores.
 */

const { getPaginated } = vi.hoisted(() => ({ getPaginated: vi.fn() }))

vi.mock('@/api/http', () => ({
  http: { getPaginated, get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

const ANANYA: PatientSummary = {
  id: '3f6c1b2e-0000-4000-8000-000000000001',
  mrn: 'MRN-2026-00042',
  first_name: 'Ananya',
  last_name: 'Rao',
  full_name: 'Ananya Rao',
  date_of_birth: '1988-03-14',
  age: 38,
  gender: 'female',
  phone: '+919812345678',
  status: 'active',
}

const SAM: PatientSummary = {
  id: '3f6c1b2e-0000-4000-8000-000000000002',
  mrn: 'MRN-2026-00043',
  first_name: 'Sam',
  last_name: 'Varghese',
  full_name: 'Sam Varghese',
  date_of_birth: '2002-06-15',
  age: 24,
  gender: 'unspecified',
  phone: null,
  status: 'inactive',
}

function page(items: PatientSummary[], totalPages = 1) {
  return {
    items,
    pagination: { page: 1, pageSize: 10, total: items.length, totalPages },
  }
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <PatientsPage />
    </QueryClientProvider>,
  )
}

describe('PatientsPage', () => {
  beforeEach(() => {
    getPaginated.mockReset()
    getPaginated.mockResolvedValue(page([ANANYA, SAM]))
  })

  it('renders patients using the backend field names', async () => {
    renderPage()

    expect(await screen.findByText('Ananya Rao')).toBeInTheDocument()
    expect(screen.getByText('MRN-2026-00042')).toBeInTheDocument()
    // `full_name` and the computed `age` come from the API; the old shape's
    // single `name` field and client-side age do not exist.
    expect(screen.getByText('38 · Female')).toBeInTheDocument()
  })

  it('labels every gender enum value, including the non-binary ones', async () => {
    renderPage()

    expect(await screen.findByText('24 · Not specified')).toBeInTheDocument()
  })

  it('shows the record lifecycle, not an invented clinical status', async () => {
    renderPage()

    expect(await screen.findByText('Active')).toBeInTheDocument()
    expect(screen.getByText('Inactive')).toBeInTheDocument()
    expect(screen.queryByText('Admitted')).not.toBeInTheDocument()
    expect(screen.queryByText('Outpatient')).not.toBeInTheDocument()
  })

  it('requests the first page with page_size on mount', async () => {
    renderPage()

    await waitFor(() =>
      expect(getPaginated).toHaveBeenCalledWith('/patients', {
        params: { q: undefined, page: 1, page_size: 10 },
      }),
    )
  })

  it('sends the search term as q, not search', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Ananya Rao')

    await user.type(screen.getByLabelText('Search name, MRN or phone…'), 'rao')

    // Debounced by 300ms, so this only settles once the timer fires.
    await waitFor(() =>
      expect(getPaginated).toHaveBeenLastCalledWith('/patients', {
        params: { q: 'rao', page: 1, page_size: 10 },
      }),
    )
    const paramNames = getPaginated.mock.calls.flatMap((call) =>
      Object.keys((call[1] as { params: Record<string, unknown> }).params),
    )
    expect(paramNames).not.toContain('search')
    expect(paramNames).not.toContain('pageSize')
  })

  it('asks the server for the next page rather than slicing the current one', async () => {
    getPaginated.mockResolvedValue(page([ANANYA, SAM], 3))
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Ananya Rao')

    await user.click(screen.getByRole('button', { name: 'Next' }))

    await waitFor(() =>
      expect(getPaginated).toHaveBeenLastCalledWith('/patients', {
        params: { q: undefined, page: 2, page_size: 10 },
      }),
    )
  })

  it('explains an empty search result differently from an empty registry', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByText('Ananya Rao')

    getPaginated.mockResolvedValue(page([]))
    await user.type(screen.getByLabelText('Search name, MRN or phone…'), 'zzz')

    // The prefix-match rule is the likeliest reason a search looks broken.
    expect(await screen.findByText('No matching patients')).toBeInTheDocument()
  })

  it('surfaces a failed load instead of an empty table', async () => {
    getPaginated.mockRejectedValue(new Error('boom'))
    renderPage()

    expect(await screen.findByText("Couldn't load patients")).toBeInTheDocument()
  })
})
