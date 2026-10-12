import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { KindPreference } from '@/api/notifications'
import { useAuthStore } from '@/store/auth-store'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import { NotificationPreferencesCard } from './NotificationPreferencesCard'

/**
 * Notification preferences, against docs/18-API_CONTRACTS.md §7.3. The real
 * hooks run against an in-memory server that applies a change the way the API
 * does — including ignoring an attempt to switch off a locked channel.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const READ = 'notification.read.own'
const UPDATE = 'notification.preference.update.own'

const catalog = (): KindPreference[] => [
  {
    kind: 'auth.password_reset_requested',
    category: 'Account',
    label: 'Password reset requested',
    critical: true,
    in_app: true,
    email: true,
    email_available: true,
    locked_channels: ['in_app', 'email'],
  },
  {
    kind: 'billing.discount_approval_requested',
    category: 'Billing',
    label: 'Discount awaiting approval',
    critical: false,
    in_app: true,
    email: false,
    email_available: true,
    locked_channels: [],
  },
  {
    kind: 'system.broadcast',
    category: 'Announcements',
    label: 'Announcements from your hospital',
    critical: false,
    in_app: true,
    email: false,
    email_available: false,
    locked_channels: [],
  },
]

let api: FakeApi
let kinds: KindPreference[]
let onRead: (() => Outcome | Promise<Outcome>) | null
let onWrite: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null

function applyChanges(config: InternalAxiosRequestConfig): Outcome {
  const { preferences } = bodyOf(config) as {
    preferences: Record<string, { in_app?: boolean; email?: boolean }>
  }
  for (const [code, channels] of Object.entries(preferences)) {
    const kind = kinds.find((k) => k.kind === code)
    if (!kind) continue
    for (const channel of ['in_app', 'email'] as const) {
      const value = channels[channel]
      // A locked channel stays as it is, whatever was asked for.
      if (value !== undefined && !kind.locked_channels.includes(channel)) kind[channel] = value
    }
  }
  return ok({ kinds: kinds.map((k) => ({ ...k })) })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  kinds = catalog()
  onRead = null
  onWrite = null
  api = installFakeApi((config) => {
    if (config.url !== '/notifications/preferences') return fail(404, 'Not found.')
    if (config.method === 'put') return (onWrite ?? applyChanges)(config)
    return onRead ? onRead() : ok({ kinds: kinds.map((k) => ({ ...k })) })
  })
})

afterEach(() => {
  api.restore()
  useAuthStore.setState({ user: null, isAuthenticated: false })
})

function renderCard(permissions: string[] = [READ, UPDATE]) {
  useAuthStore.setState({
    user: { id: 'u1', name: 'Test User', email: 'user@example.com', permissions },
    isAuthenticated: true,
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <NotificationPreferencesCard />
    </QueryClientProvider>,
  )
}

const toggle = (name: string) => screen.findByRole('switch', { name })
const puts = () => api.requests('put')

describe('NotificationPreferencesCard', () => {
  it('draws every kind the API returns, grouped by its category', async () => {
    renderCard()

    const billing = await screen.findByRole('region', { name: 'Billing' })
    expect(within(billing).getByText('Discount awaiting approval')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Account' })).getByText('Password reset requested')).toBeInTheDocument()
    expect(
      within(screen.getByRole('region', { name: 'Announcements' })).getByText('Announcements from your hospital'),
    ).toBeInTheDocument()

    // The switches show what is in effect.
    expect(await toggle('Discount awaiting approval: In-app')).toBeChecked()
    expect(await toggle('Discount awaiting approval: Email')).not.toBeChecked()

    const [request] = api.requests('get')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications/preferences')
  })

  it('locks the channels of a critical kind', async () => {
    renderCard()

    expect(await toggle('Password reset requested: In-app')).toBeDisabled()
    expect(await toggle('Password reset requested: Email')).toBeDisabled()
    expect(await toggle('Password reset requested: Email')).toBeChecked()
    expect(within(screen.getByRole('region', { name: 'Account' })).getByText('Always on')).toBeInTheDocument()
  })

  it('offers no email switch for a kind with no email form', async () => {
    renderCard()

    expect(await toggle('Announcements from your hospital: In-app')).toBeEnabled()
    expect(
      screen.queryByRole('switch', { name: 'Announcements from your hospital: Email' }),
    ).not.toBeInTheDocument()
  })

  it('does not claim email delivery is working', async () => {
    renderCard()

    expect(
      await screen.findByText(/Email is sent only if your hospital has email delivery set up/),
    ).toBeInTheDocument()
  })

  it('saves one change, sending only that kind and channel', async () => {
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: Email'))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Notification preference saved'))
    const [request] = puts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications/preferences')
    expect(bodyOf(request)).toEqual({
      preferences: { 'billing.discount_approval_requested': { email: true } },
    })
    expect(await toggle('Discount awaiting approval: Email')).toBeChecked()
    // Untouched preferences stay as they were.
    expect(await toggle('Discount awaiting approval: In-app')).toBeChecked()
  })

  it('switches a channel off', async () => {
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Announcements from your hospital: In-app'))

    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(bodyOf(puts()[0])).toEqual({ preferences: { 'system.broadcast': { in_app: false } } })
    await waitFor(async () =>
      expect(await toggle('Announcements from your hospital: In-app')).not.toBeChecked(),
    )
  })

  it('shows what the server says is in effect, not what was asked for', async () => {
    // The server accepts the request but keeps the channel on.
    onWrite = () => ok({ kinds: kinds.map((k) => ({ ...k })) })
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: In-app'))

    await waitFor(() => expect(puts()).toHaveLength(1))
    expect(await toggle('Discount awaiting approval: In-app')).toBeChecked()
  })

  it('holds every switch while one change is being saved', async () => {
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(applyChanges(config))))
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: Email'))

    const saving = await toggle('Discount awaiting approval: Email')
    await waitFor(() => expect(saving).toBeDisabled())
    expect(saving).toHaveAttribute('aria-busy', 'true')
    expect(await toggle('Announcements from your hospital: In-app')).toBeDisabled()
    await user.click(saving)
    expect(puts()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(await toggle('Discount awaiting approval: Email')).toBeEnabled()
    expect(await toggle('Discount awaiting approval: Email')).toBeChecked()
  })

  it('leaves the switch as it was when saving fails', async () => {
    onWrite = () => fail(500, 'Traceback (most recent call last)…')
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: Email'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't save that preference. Please try again."),
    )
    expect(await toggle('Discount awaiting approval: Email')).not.toBeChecked()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('shows a validation message from the API', async () => {
    onWrite = () =>
      fail(422, 'Unknown notification kind: billing.discount_approval_requested.', {
        error_code: 'VALIDATION_ERROR',
      })
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: Email'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        'Unknown notification kind: billing.discount_approval_requested.',
      ),
    )
  })

  it('explains a permission refusal from the API', async () => {
    onWrite = () => fail(403, 'Permission denied.', { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    renderCard()

    await user.click(await toggle('Discount awaiting approval: Email'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith(
        "You don't have permission to change notification preferences.",
      ),
    )
  })

  it('is read-only without the update permission', async () => {
    renderCard([READ])

    expect(await toggle('Discount awaiting approval: Email')).toBeDisabled()
    expect(await toggle('Announcements from your hospital: In-app')).toBeDisabled()
    expect(screen.getByText(/can view these preferences but not change them/)).toBeInTheDocument()
  })

  it('renders nothing, and asks for nothing, without the read permission', async () => {
    renderCard([])

    expect(screen.queryByText('Notifications')).not.toBeInTheDocument()
    await waitFor(() => expect(api.sent).toHaveLength(0))
  })

  it('shows a loading state, then the preferences', async () => {
    let finish: () => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (finish = () => resolve(ok({ kinds }))))
    renderCard()

    expect(await screen.findByRole('status', { name: 'Loading notification preferences' })).toBeInTheDocument()
    finish()
    expect(await toggle('Discount awaiting approval: In-app')).toBeInTheDocument()
  })

  it('offers a retry when the preferences fail to load', async () => {
    onRead = () => fail(500, 'Internal error.')
    const user = userEvent.setup()
    renderCard()

    expect(await screen.findByText("Couldn't load your notification preferences")).toBeInTheDocument()
    onRead = null
    await user.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await toggle('Discount awaiting approval: In-app')).toBeInTheDocument()
  })
})
