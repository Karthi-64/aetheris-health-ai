import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import UsersPage from './UsersPage'

const { getPaginated } = vi.hoisted(() => ({ getPaginated: vi.fn() }))
vi.mock('@/api/http', () => ({ http: { getPaginated, get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() } }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

function user(n: number) {
  return {
    id: `u${n}`,
    email: `staff${n}@hospital.test`,
    first_name: `Staff${n}`,
    last_name: 'Member',
    phone: null,
    status: 'active',
    hospital_id: 'h1',
    roles: [],
    mfa_enabled: false,
    last_login_at: null,
    password_changed_at: null,
    created_at: null,
    updated_at: null,
  }
}

/** The users query is the only paginated call the page makes. */
function usersCalls() {
  return getPaginated.mock.calls.filter((c) => c[0] === '/users')
}

function lastUsersParams() {
  const calls = usersCalls()
  return calls[calls.length - 1][1].params
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UsersPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('UsersPage directory', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getPaginated.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/users'
          ? {
              items: Array.from({ length: 10 }, (_, i) => user(i + 1)),
              pagination: { page: 1, pageSize: 10, total: 42, totalPages: 5 },
            }
          : { items: [], pagination: { page: 1, pageSize: 100, total: 0, totalPages: 0 } },
      ),
    )
  })

  it('asks the server for an explicit page rather than relying on the default', async () => {
    renderPage()
    await waitFor(() => expect(usersCalls().length).toBeGreaterThan(0))
    expect(lastUsersParams()).toMatchObject({ page: 1, page_size: 10 })
  })

  it('pages through the whole directory, not just the first response', async () => {
    const actor = userEvent.setup()
    renderPage()

    // 42 records across 5 pages — the footer must reflect the server's count,
    // not the number of rows currently in the table.
    expect(await screen.findByText(/page 1 of 5/i)).toBeInTheDocument()

    await actor.click(screen.getByRole('button', { name: /next/i }))
    await waitFor(() => expect(lastUsersParams().page).toBe(2))
  })

  it('sends the search term to the API so it spans every page', async () => {
    const actor = userEvent.setup()
    renderPage()
    await waitFor(() => expect(usersCalls().length).toBeGreaterThan(0))

    await actor.type(screen.getByPlaceholderText(/search name or email/i), 'asha')

    // Debounced: the request carries the term once typing settles.
    await waitFor(() => expect(lastUsersParams().search).toBe('asha'), { timeout: 2000 })
  })

  it('returns to page 1 when the filters change', async () => {
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await screen.findByRole('button', { name: /next/i }))
    await waitFor(() => expect(lastUsersParams().page).toBe(2))

    await actor.type(screen.getByPlaceholderText(/search name or email/i), 'asha')
    await waitFor(() => expect(lastUsersParams().page).toBe(1), { timeout: 2000 })
  })

  it('passes the status filter to the server', async () => {
    renderPage()
    await waitFor(() => expect(usersCalls().length).toBeGreaterThan(0))
    expect(lastUsersParams().status).toBeUndefined()
  })
})
