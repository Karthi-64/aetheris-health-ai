import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { Notification } from '@/api/notifications'
import { useAuthStore } from '@/store/auth-store'
import { fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import { NotificationBell } from './NotificationBell'

/**
 * The bell and the notification centre, against docs/18-API_CONTRACTS.md §7.2.
 *
 * The real hooks, cache and Axios instance run against an in-memory server
 * that holds the notifications and answers the way the API does: the unread
 * count is derived from them, marking one read changes it, and so on. Each
 * test asserts on the request that left and on what the centre then shows.
 */

const { toastError } = vi.hoisted(() => ({ toastError: vi.fn() }))
vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: toastError } }))

// Every seeded role holds both `.own` codes.
const STAFF = ['notification.read.own', 'notification.preference.update.own']

const minutesAgo = (n: number) => new Date(Date.now() - n * 60_000).toISOString()

function notification(overrides: Partial<Notification>): Notification {
  return {
    id: 'n-x',
    kind: 'system.broadcast',
    title: 'Title',
    body: 'Body',
    link: null,
    is_read: false,
    read_at: null,
    created_at: minutesAgo(5),
    ...overrides,
  }
}

const DISCOUNT = () =>
  notification({
    id: 'n-discount',
    kind: 'billing.discount_approval_requested',
    title: 'Discount awaiting your approval',
    body: 'Priya Sharma applied a discount of INR 200.00 to an invoice for Ananya Rao.',
    link: '/billing',
    created_at: minutesAgo(5),
  })
const MAINTENANCE = () =>
  notification({
    id: 'n-maintenance',
    kind: 'system.broadcast',
    title: 'Scheduled maintenance tonight',
    body: 'The system will be unavailable from 23:00 to 23:30.',
    created_at: minutesAgo(90),
  })
const RESET = () =>
  notification({
    id: 'n-reset',
    kind: 'auth.password_reset_requested',
    title: 'Password reset requested',
    body: 'A password reset was requested for your account.',
    is_read: true,
    read_at: minutesAgo(60 * 24),
    created_at: minutesAgo(60 * 26),
  })

let api: FakeApi
/** Newest first, as the API returns them. */
let server: Notification[]
let onList: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
let onWrite: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null

function listPage(config: InternalAxiosRequestConfig): Outcome {
  const { unread_only: unreadOnly, page = 1, page_size: pageSize = 25 } = (config.params ?? {}) as {
    unread_only?: boolean
    page?: number
    page_size?: number
  }
  const rows = unreadOnly ? server.filter((n) => !n.is_read) : server
  return {
    status: 200,
    data: {
      success: true,
      message: 'Notifications retrieved.',
      data: rows.slice((page - 1) * pageSize, page * pageSize).map((n) => ({ ...n })),
      metadata: {
        pagination: {
          page,
          page_size: pageSize,
          total_records: rows.length,
          total_pages: Math.max(1, Math.ceil(rows.length / pageSize)),
        },
      },
    },
  }
}

function write(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  if (url === '/notifications/read-all') {
    const unread = server.filter((n) => !n.is_read)
    for (const n of unread) Object.assign(n, { is_read: true, read_at: new Date().toISOString() })
    return ok({ marked: unread.length })
  }
  const id = /^\/notifications\/([^/]+)\/read$/.exec(url)?.[1]
  const found = server.find((n) => n.id === id)
  if (!found) return fail(404, 'Notification not found.', { error_code: 'RESOURCE_NOT_FOUND' })
  if (!found.is_read) Object.assign(found, { is_read: true, read_at: new Date().toISOString() })
  return ok({ ...found })
}

beforeEach(() => {
  toastError.mockReset()
  server = [DISCOUNT(), MAINTENANCE(), RESET()]
  onList = null
  onWrite = null
  api = installFakeApi((config) => {
    const url = config.url ?? ''
    if (config.method === 'post') return (onWrite ?? write)(config)
    if (url === '/notifications/unread-count') {
      return ok({ unread: server.filter((n) => !n.is_read).length })
    }
    if (url === '/notifications') return (onList ?? listPage)(config)
    return fail(404, 'Not found.')
  })
})

afterEach(() => {
  api.restore()
  useAuthStore.setState({ user: null, isAuthenticated: false })
})

function Where() {
  return <p data-testid="where">{useLocation().pathname}</p>
}

function renderBell(permissions: string[] = STAFF) {
  useAuthStore.setState({
    user: { id: 'u1', name: 'Test User', email: 'user@example.com', permissions },
    isAuthenticated: true,
  })
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/dashboard']}>
      <QueryClientProvider client={client}>
        <NotificationBell />
        <Where />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const bell = () => screen.getByRole('button', { name: /^Notifications/ })
const bellWith = (name: string) => screen.findByRole('button', { name })
const posts = () => api.requests('post')
const listRequests = () => api.requests('get').filter((c) => c.url === '/notifications')
const countRequests = () => api.requests('get', '/notifications/unread-count')

async function openCentre(user: ReturnType<typeof userEvent.setup>) {
  await user.click(bell())
  return screen.findByRole('dialog', { name: 'Notifications' })
}

const itemFor = (centre: HTMLElement, title: string) =>
  within(centre)
    .getAllByRole('listitem')
    .find((li) => within(li).queryByText(title)) as HTMLElement

describe('NotificationBell', () => {
  it('shows the unread count on the bell and in its label', async () => {
    renderBell()

    const withCount = await bellWith('Notifications, 2 unread')
    expect(withCount).toHaveTextContent('2')
    expect(withCount).toBeEnabled()
    const [request] = countRequests()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications/unread-count')
  })

  it('shows no badge when nothing is unread', async () => {
    server = [RESET()]
    renderBell()

    await waitFor(() => expect(bell()).toHaveAttribute('aria-busy', 'false'))
    expect(bell()).toHaveAccessibleName('Notifications')
    expect(bell()).toHaveTextContent('')
  })

  it('is marked busy until the first count arrives', async () => {
    renderBell()

    expect(bell()).toHaveAttribute('aria-busy', 'true')
    expect(bell()).toHaveAccessibleName('Notifications')
    await bellWith('Notifications, 2 unread')
    expect(bell()).toHaveAttribute('aria-busy', 'false')
  })

  it('caps a large count', async () => {
    server = Array.from({ length: 120 }, (_, i) => notification({ id: `n-${i}` }))
    renderBell()

    expect(await bellWith('Notifications, 120 unread')).toHaveTextContent('99+')
  })

  it('renders nothing, and asks for nothing, without notification.read.own', async () => {
    renderBell([])

    expect(screen.queryByRole('button', { name: /Notifications/ })).not.toBeInTheDocument()
    await waitFor(() => expect(api.sent).toHaveLength(0))
  })

  it('re-asks for the count on an interval, since there is no push channel', async () => {
    vi.useFakeTimers()
    try {
      renderBell()
      // Long enough for the first response to land, far short of a poll.
      await act(() => vi.advanceTimersByTimeAsync(1_000))
      expect(countRequests()).toHaveLength(1)
      expect(bell()).toHaveAccessibleName('Notifications, 2 unread')

      // A new announcement arrives while the user is on some other screen.
      server.unshift(notification({ id: 'n-new', title: 'Fire drill at 3pm' }))
      await act(() => vi.advanceTimersByTimeAsync(60_000))

      expect(countRequests()).toHaveLength(2)
      expect(bell()).toHaveAccessibleName('Notifications, 3 unread')
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('notification centre', () => {
  it('lists the notifications the API returned, newest first', async () => {
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    const items = await within(centre).findAllByRole('listitem')
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('Discount awaiting your approval')
    expect(items[1]).toHaveTextContent('Scheduled maintenance tonight')
    expect(items[2]).toHaveTextContent('Password reset requested')

    const discount = itemFor(centre, 'Discount awaiting your approval')
    expect(
      within(discount).getByText(
        'Priya Sharma applied a discount of INR 200.00 to an invoice for Ananya Rao.',
      ),
    ).toBeInTheDocument()
    // Its category, and when it was raised.
    expect(discount).toHaveTextContent('Billing')
    const time = discount.querySelector('time') as HTMLTimeElement
    expect(time).toHaveAttribute('datetime', server[0].created_at)
    expect(time.textContent).toBe(
      new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' }).format(-5, 'minute'),
    )
    expect(itemFor(centre, 'Scheduled maintenance tonight')).toHaveTextContent('Announcements')
    expect(itemFor(centre, 'Password reset requested')).toHaveTextContent('Account')

    const [request] = listRequests()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications')
    expect(request.params).toEqual({ page: 1, page_size: 20 })
  })

  it('renders title and body as text, never as markup', async () => {
    server = [notification({ id: 'n-html', title: '<b>Bold</b>', body: '<img src=x onerror=alert(1)>' })]
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findByText('<b>Bold</b>')).toBeInTheDocument()
    expect(within(centre).getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(centre.querySelector('img')).toBeNull()
  })

  it('still shows a kind this build does not know', async () => {
    server = [notification({ id: 'n-lab', kind: 'lab.result_ready', title: 'Lab result ready' })]
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findByText('Lab result ready')).toBeInTheDocument()
  })

  it('marks unread notifications apart from read ones', async () => {
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    const unread = itemFor(centre, 'Scheduled maintenance tonight')
    const read = itemFor(centre, 'Password reset requested')

    expect(within(unread).getByText('Unread:')).toBeInTheDocument()
    expect(within(unread).getByText('Scheduled maintenance tonight')).toHaveClass('font-bold')
    expect(within(read).queryByText('Unread:')).not.toBeInTheDocument()
    expect(within(read).getByText('Password reset requested')).not.toHaveClass('font-bold')
    // A read notification with nowhere to go is not a control.
    expect(within(read).queryByRole('button')).not.toBeInTheDocument()
    expect(within(read).queryByRole('link')).not.toBeInTheDocument()
  })

  it('shows a loading state while the list is on its way', async () => {
    let finish: () => void = () => {}
    onList = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(listPage(config))))
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(within(centre).getByRole('status', { name: 'Loading notifications' })).toBeInTheDocument()
    expect(within(centre).queryByRole('listitem')).not.toBeInTheDocument()

    finish()
    expect(await within(centre).findAllByRole('listitem')).toHaveLength(3)
    expect(within(centre).queryByRole('status')).not.toBeInTheDocument()
  })

  it('shows an empty state when there are no notifications', async () => {
    server = []
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findByText('No notifications yet')).toBeInTheDocument()
    expect(within(centre).queryByRole('button', { name: /Mark all as read/ })).not.toBeInTheDocument()
  })

  it('handles a failed load with a retry, without showing server internals', async () => {
    onList = () => fail(500, 'Traceback (most recent call last): asyncpg…')
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findByText(/Notifications couldn't be loaded/)).toBeInTheDocument()
    expect(within(centre).queryByText(/Traceback/)).not.toBeInTheDocument()

    onList = null
    await user.click(within(centre).getByRole('button', { name: /Retry/ }))
    expect(await within(centre).findAllByRole('listitem')).toHaveLength(3)
  })

  it('explains a permission refusal and offers no retry', async () => {
    onList = () => fail(403, 'Permission denied.', { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findByText("You don't have access to notifications.")).toBeInTheDocument()
    expect(within(centre).queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('filters to unread using the query the API reads', async () => {
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(centre).getByRole('button', { name: 'Unread (2)' }))

    await waitFor(() => expect(listRequests().at(-1)?.params).toEqual({ unread_only: true, page: 1, page_size: 20 }))
    await waitFor(() => expect(within(centre).getAllByRole('listitem')).toHaveLength(2))
    expect(within(centre).queryByText('Password reset requested')).not.toBeInTheDocument()
    expect(within(centre).getByRole('button', { name: 'Unread (2)' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('says so when the unread filter has nothing left', async () => {
    server = [RESET()]
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(centre).getByRole('button', { name: 'Unread' }))

    expect(await within(centre).findByText("You're all caught up")).toBeInTheDocument()
  })

  it('loads older notifications a page at a time', async () => {
    server = Array.from({ length: 25 }, (_, i) =>
      notification({ id: `n-${i}`, title: `Announcement ${i + 1}`, created_at: minutesAgo(i + 1) }),
    )
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    expect(await within(centre).findAllByRole('listitem')).toHaveLength(20)

    await user.click(within(centre).getByRole('button', { name: 'Show older notifications' }))

    await waitFor(() => expect(within(centre).getAllByRole('listitem')).toHaveLength(25))
    expect(listRequests().at(-1)?.params).toEqual({ page: 2, page_size: 20 })
    expect(within(centre).getByText('Announcement 25')).toBeInTheDocument()
    expect(within(centre).queryByRole('button', { name: 'Show older notifications' })).not.toBeInTheDocument()
  })

  it('links to notification preferences', async () => {
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await user.click(within(centre).getByRole('link', { name: /Notification preferences/ }))

    expect(screen.getByTestId('where')).toHaveTextContent('/settings/profile')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('can be opened and closed from the keyboard', async () => {
    const user = userEvent.setup()
    renderBell()
    await bellWith('Notifications, 2 unread')

    bell().focus()
    await user.keyboard('{Enter}')
    const centre = await screen.findByRole('dialog', { name: 'Notifications' })
    await within(centre).findAllByRole('listitem')
    // Focus moves into the centre.
    expect(centre.contains(document.activeElement)).toBe(true)

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(bell()).toHaveFocus()
  })
})

describe('marking notifications read', () => {
  it('marks one read, updating the row and the bell', async () => {
    const user = userEvent.setup()
    renderBell()
    await bellWith('Notifications, 2 unread')

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(itemFor(centre, 'Scheduled maintenance tonight')).getByRole('button'))

    await waitFor(() => expect(posts()).toHaveLength(1))
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications/n-maintenance/read')
    // No request body.
    expect(request.data).toBeUndefined()

    expect(await bellWith('Notifications, 1 unread')).toHaveTextContent('1')
    const row = itemFor(centre, 'Scheduled maintenance tonight')
    await waitFor(() => expect(within(row).queryByText('Unread:')).not.toBeInTheDocument())
    expect(within(row).getByText('Scheduled maintenance tonight')).not.toHaveClass('font-bold')
    // Still open: there was nowhere to navigate to.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(screen.getByTestId('where')).toHaveTextContent('/dashboard')
  })

  it('opens a notification with a link: marks it read and goes there', async () => {
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    const link = within(itemFor(centre, 'Discount awaiting your approval')).getByRole('link')
    expect(link).toHaveAttribute('href', '/billing')
    await user.click(link)

    expect(screen.getByTestId('where')).toHaveTextContent('/billing')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(posts()).toHaveLength(1))
    expect(posts()[0].url).toBe('/notifications/n-discount/read')
    expect(await bellWith('Notifications, 1 unread')).toBeInTheDocument()
  })

  it('follows the link of an already-read notification without re-marking it', async () => {
    server = [{ ...DISCOUNT(), is_read: true, read_at: minutesAgo(1) }]
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await user.click(await within(centre).findByRole('link', { name: /Discount awaiting your approval/ }))

    expect(screen.getByTestId('where')).toHaveTextContent('/billing')
    expect(posts()).toHaveLength(0)
  })

  it('does not follow a link that is not an in-app path', async () => {
    server = [
      notification({ id: 'n-ext', title: 'External', link: 'https://example.com/phish' }),
      notification({ id: 'n-rel', title: 'Protocol relative', link: '//example.com' }),
    ]
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    expect(within(centre).queryAllByRole('link', { name: /External|Protocol relative/ })).toHaveLength(0)

    // It can still be marked read; it just goes nowhere.
    await user.click(within(itemFor(centre, 'External')).getByRole('button'))
    await waitFor(() => expect(posts()).toHaveLength(1))
    expect(screen.getByTestId('where')).toHaveTextContent('/dashboard')
  })

  it('marks everything read with the read-all endpoint', async () => {
    const user = userEvent.setup()
    renderBell()
    await bellWith('Notifications, 2 unread')

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(centre).getByRole('button', { name: /Mark all as read/ }))

    await waitFor(() => expect(posts()).toHaveLength(1))
    const [request] = posts()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/notifications/read-all')
    expect(request.data).toBeUndefined()

    await waitFor(() => expect(bell()).toHaveAccessibleName('Notifications'))
    expect(within(centre).queryByText('Unread:')).not.toBeInTheDocument()
    // Nothing left to mark.
    expect(within(centre).queryByRole('button', { name: /Mark all as read/ })).not.toBeInTheDocument()
  })

  it('puts a notification back as unread when marking it fails', async () => {
    onWrite = () => fail(500, 'Internal error.')
    const user = userEvent.setup()
    renderBell()
    await bellWith('Notifications, 2 unread')

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(itemFor(centre, 'Scheduled maintenance tonight')).getByRole('button'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't mark that notification as read. Please try again."),
    )
    expect(await bellWith('Notifications, 2 unread')).toBeInTheDocument()
    await waitFor(() =>
      expect(within(itemFor(centre, 'Scheduled maintenance tonight')).getByText('Unread:')).toBeInTheDocument(),
    )
  })

  it('reports a failed mark-all and leaves the count alone', async () => {
    onWrite = () => fail(500, 'Internal error.')
    const user = userEvent.setup()
    renderBell()

    const centre = await openCentre(user)
    await within(centre).findAllByRole('listitem')
    await user.click(within(centre).getByRole('button', { name: /Mark all as read/ }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't mark notifications as read. Please try again."),
    )
    expect(bell()).toHaveAccessibleName('Notifications, 2 unread')
  })
})
