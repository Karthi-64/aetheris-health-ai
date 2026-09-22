import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RegisterPatientDialog } from './RegisterPatientDialog'

/**
 * Registration against the real create contract
 * (`docs/18-API_CONTRACTS.md` §2.3): first and last name, a date of birth, and
 * a backend gender value. The form must not offer age, department or `M`/`F` —
 * the API rejects all three.
 */

const { post } = vi.hoisted(() => ({ post: vi.fn() }))

vi.mock('@/api/http', () => ({
  http: { post, get: vi.fn(), getPaginated: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <RegisterPatientDialog trigger={<button type="button">Register Patient</button>} />
    </QueryClientProvider>,
  )
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: 'Register Patient' }))
  await screen.findByRole('dialog')
}

describe('RegisterPatientDialog', () => {
  beforeEach(() => {
    post.mockReset()
    post.mockResolvedValue({
      id: '1',
      mrn: 'MRN-2026-00044',
      full_name: 'Ananya Rao',
    })
  })

  it('collects the fields the API accepts and none it rejects', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    expect(screen.getByLabelText(/First name/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Last name/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Date of birth/)).toBeInTheDocument()
    // Age and department were on the mock form; the backend has neither.
    expect(screen.queryByLabelText(/Age/)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/Department/)).not.toBeInTheDocument()
  })

  it('reports validation errors instead of posting an incomplete record', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.click(screen.getByRole('button', { name: 'Register' }))

    expect(await screen.findByText('First name is required')).toBeInTheDocument()
    expect(screen.getByText('Last name is required')).toBeInTheDocument()
    expect(screen.getByText('Date of birth is required')).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('rejects a date of birth in the future, as the backend would', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/First name/), 'Ananya')
    await user.type(screen.getByLabelText(/Last name/), 'Rao')
    await user.type(screen.getByLabelText(/Date of birth/), '2099-01-01')
    await user.click(screen.getByRole('button', { name: 'Register' }))

    expect(
      await screen.findByText('Enter a date in the past, within the last 130 years'),
    ).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('posts snake_case fields with a date of birth and a backend gender value', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/First name/), 'Ananya')
    await user.type(screen.getByLabelText(/Last name/), 'Rao')
    await user.type(screen.getByLabelText(/Date of birth/), '1988-03-14')
    await user.click(screen.getByRole('combobox', { name: /Gender/ }))
    await user.click(await screen.findByRole('option', { name: 'Female' }))
    await user.click(screen.getByRole('button', { name: 'Register' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/patients', {
        first_name: 'Ananya',
        last_name: 'Rao',
        date_of_birth: '1988-03-14',
        gender: 'female',
      }),
    )
    // No MRN: the server allocates it.
    expect(post.mock.calls[0][1]).not.toHaveProperty('mrn')
  })

  it('omits blank optional fields rather than sending empty strings', async () => {
    const user = userEvent.setup()
    renderDialog()
    await open(user)

    await user.type(screen.getByLabelText(/First name/), 'Ravi')
    await user.type(screen.getByLabelText(/Last name/), 'Menon')
    await user.type(screen.getByLabelText(/Date of birth/), '1970-05-05')
    await user.click(screen.getByRole('combobox', { name: /Gender/ }))
    await user.click(await screen.findByRole('option', { name: 'Male' }))
    await user.click(screen.getByRole('button', { name: 'Register' }))

    await waitFor(() => expect(post).toHaveBeenCalled())
    const body = post.mock.calls[0][1] as Record<string, unknown>
    expect(body).not.toHaveProperty('phone')
    expect(body).not.toHaveProperty('email')
    expect(body).not.toHaveProperty('blood_group')
  })
})
