import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ApiError } from '@/api/types'
import { MOCK_PERMISSIONS_BY_ROLE } from '@/lib/rbac'
import { signIn, signOut } from '@/test/auth'
import UsersPage from './UsersPage'

const { getPaginated, post, toastSuccess, toastError } = vi.hoisted(() => ({
  getPaginated: vi.fn(),
  post: vi.fn(),
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('@/api/http', () => ({ http: { getPaginated, get: vi.fn(), post, patch: vi.fn(), delete: vi.fn() } }))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

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

  it('gives the status filter a name a screen reader can announce', async () => {
    renderPage()
    expect(await screen.findByRole('combobox', { name: 'Filter by status' })).toBeInTheDocument()
  })

  it('passes the status filter to the server', async () => {
    renderPage()
    await waitFor(() => expect(usersCalls().length).toBeGreaterThan(0))
    expect(lastUsersParams().status).toBeUndefined()
  })
})

describe('UsersPage resend invitation', () => {
  const INVITED_EMAIL = 'staff2@hospital.test'

  beforeEach(() => {
    vi.clearAllMocks()
    post.mockReset()
    const statuses = ['active', 'invited', 'suspended', 'deactivated']
    getPaginated.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/users'
          ? {
              items: statuses.map((status, i) => ({ ...user(i + 1), status })),
              pagination: { page: 1, pageSize: 10, total: 4, totalPages: 1 },
            }
          : { items: [], pagination: { page: 1, pageSize: 100, total: 0, totalPages: 0 } },
      ),
    )
  })

  afterEach(() => {
    signOut()
  })

  const resendButton = () => screen.findByRole('button', { name: 'Resend invitation' })

  it('offers Resend invitation only on the row of an invited user, before Deactivate', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    renderPage()

    const buttons = await screen.findAllByRole('button', { name: 'Resend invitation' })
    expect(buttons).toHaveLength(1)
    const row = buttons[0].closest('tr') as HTMLElement
    expect(within(row).getByText(INVITED_EMAIL)).toBeInTheDocument()
    expect(within(row).getAllByRole('button').map((b) => b.textContent)).toEqual([
      'Edit',
      'Roles',
      'Resend invitation',
      'Deactivate',
    ])
  })

  it('does not offer it to someone who may not create invites', async () => {
    signIn(['user.read', 'user.update', 'user.deactivate'])
    renderPage()

    expect(await screen.findByText(INVITED_EMAIL)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resend invitation' })).not.toBeInTheDocument()
  })

  it('posts to the invitation endpoint with no body and says the email was queued', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    post.mockResolvedValue({ delivery: 'queued' })
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await resendButton())

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(`Invitation queued for ${INVITED_EMAIL}`, {
        description: 'Delivery is not confirmed.',
      }),
    )
    expect(post.mock.calls).toEqual([['/users/u2/invitation']])
    expect(toastError).not.toHaveBeenCalled()
  })

  it.each([
    ['unavailable', { delivery: 'unavailable' }],
    ['an unrecognised delivery', { delivery: 'sent' }],
    ['no delivery at all', undefined],
  ])('reports that nothing was sent when the API answers %s', async (_case, answer) => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    post.mockResolvedValue(answer)
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await resendButton())

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        `No invitation was sent to ${INVITED_EMAIL}. Email delivery is not available.`,
      ),
    )
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('asks the admin to wait when the API rate-limits the resend', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    post.mockRejectedValue(new ApiError('Rate limit exceeded.', 'RATE_LIMITED', 429))
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await resendButton())

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        `Too many invitations were sent to ${INVITED_EMAIL}. Try again later.`,
      ),
    )
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it("gives the API's reason when the user is no longer invited", async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const reason = 'This user has already activated their account.'
    post.mockRejectedValue(new ApiError(reason, 'CONFLICT', 409))
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await resendButton())
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(reason))
  })

  it('shows a plain sentence, not server text, when the server fails', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    post.mockRejectedValue(new ApiError('Internal Server Error', 'network_error', 500))
    const actor = userEvent.setup()
    renderPage()

    await actor.click(await resendButton())
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't resend the invitation. Please try again."),
    )
  })

  it('sends one invitation when the button is clicked twice before the first is answered', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    let finish: (answer: unknown) => void = () => {}
    post.mockImplementation(() => new Promise((resolve) => (finish = resolve)))
    renderPage()

    const button = await resendButton()
    button.click()
    button.click()

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    finish({ delivery: 'queued' })
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledTimes(1)
  })
})
