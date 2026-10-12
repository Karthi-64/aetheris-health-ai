import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { AuditLogEntry } from '@/api/audit'
import { signIn, signOut } from '@/test/auth'
import { fail, installFakeApi, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import { AuditLogTab } from './AuditLogTab'

/**
 * The audit tab and its export, against `GET /api/v1/audit-logs` and
 * `GET /api/v1/audit-logs/export` (`backend/app/api/v1/audit.py`). The real
 * hooks, permission check and Axios instance run; only the network adapter is
 * replaced, and the browser's download is observed at the two points it
 * touches: the object URL made for the file and the link that is clicked.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Seeded roles (backend/app/seeds/seed.py): only Super Admin holds audit.export.
const SUPER_ADMIN = ['audit.read', 'audit.export']
const HOSPITAL_ADMIN = ['audit.read']

function entry(id: string, action: string): AuditLogEntry {
  return {
    id,
    action,
    actor_id: 'u1',
    actor_name: 'Asha Verma',
    actor_email: 'asha@example.com',
    actor_type: 'user',
    target_type: 'patient',
    target_id: '11111111-2222-3333-4444-555555555555',
    before: null,
    after: null,
    context: null,
    created_at: '2026-10-05T10:15:00Z',
  }
}

const CSV = 'id,created_at,action\n1,2026-10-05T10:15:00+00:00,patient.created\n'

const file = (body: string, type: string, filename?: string): Outcome => ({
  status: 200,
  data: new Blob([body], { type }),
  headers: filename ? { 'content-disposition': `attachment; filename="${filename}"` } : {},
})

let rows: AuditLogEntry[]
let total: number | undefined
let onExport: (config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>
let fake: FakeApi
/** What the page handed to the browser: one entry per link clicked. */
let downloads: { filename: string; blob: Blob }[]

const realCreate = URL.createObjectURL
const realRevoke = URL.revokeObjectURL

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  rows = [entry('e1', 'patient.created'), entry('e2', 'patient.updated'), entry('e3', 'user.invited')]
  total = undefined
  onExport = () => file(CSV, 'text/csv', 'audit-logs-20261005-101500.csv')
  fake = installFakeApi((config) => {
    if (config.url === '/audit-logs/export') return onExport(config)
    if (config.url === '/audit-logs') return paged(rows, total ?? rows.length)
    return paged([])
  })

  downloads = []
  const held = new Map<string, Blob>()
  URL.createObjectURL = vi.fn((blob: Blob) => {
    const url = `blob:test/${held.size}`
    held.set(url, blob)
    return url
  }) as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
    const blob = held.get(this.href)
    if (blob) downloads.push({ filename: this.download, blob })
  })
})

afterEach(() => {
  fake.restore()
  signOut()
  vi.restoreAllMocks()
  URL.createObjectURL = realCreate
  URL.revokeObjectURL = realRevoke
})

function renderTab(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <AuditLogTab />
    </QueryClientProvider>,
  )
}

const exports = () => fake.requests('get', '/audit-logs/export')
const lists = () => fake.sent.filter((c) => c.method === 'get' && c.url === '/audit-logs')
/** Query parameters as they go on the wire: unset ones are not sent. */
const sentParams = (config: InternalAxiosRequestConfig) =>
  JSON.parse(JSON.stringify(config.params)) as Record<string, unknown>

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByText('patient.created')
  await user.click(screen.getByRole('button', { name: 'Export' }))
  return screen.findByRole('dialog', { name: 'Export audit log' })
}

describe('audit log', () => {
  it('lists the entries the API returns', async () => {
    renderTab(HOSPITAL_ADMIN)

    expect(await screen.findByText('patient.created')).toBeInTheDocument()
    expect(screen.getByText('user.invited')).toBeInTheDocument()
    expect(screen.getAllByText('Asha Verma')).toHaveLength(3)
    expect(sentParams(lists()[0])).toEqual({ page: 1, page_size: 10 })
  })

  it('sends the filters to the server', async () => {
    renderTab(HOSPITAL_ADMIN)
    await screen.findByText('patient.created')

    fireEvent.change(screen.getByLabelText(/^Action/), { target: { value: 'patient.created' } })

    await waitFor(() => expect(sentParams(lists().at(-1)!)).toMatchObject({ action: 'patient.created', page: 1 }))
  })
})

describe('audit log controls', () => {
  it("names each row's View button after the entry it opens", async () => {
    rows = [
      entry('e1', 'patient.created'),
      { ...entry('e2', 'patient.updated'), actor_name: null, created_at: '2026-10-05T11:20:00Z' },
    ]
    const user = userEvent.setup()
    renderTab(HOSPITAL_ADMIN)
    await screen.findByText('patient.created')

    const names = screen.getAllByRole('button', { name: /^View / }).map((b) => b.getAttribute('aria-label'))
    expect(names).toHaveLength(2)
    expect(new Set(names).size).toBe(2)
    expect(names[0]).toMatch(/^View patient\.created by Asha Verma at .+/)
    expect(names[1]).toMatch(/^View patient\.updated by System at .+/)

    await user.click(screen.getByRole('button', { name: /^View patient\.updated/ }))
    expect(await screen.findByRole('dialog', { name: 'patient.updated' })).toBeInTheDocument()
  })

  it('has one search box, not two bound to the same value', async () => {
    renderTab(HOSPITAL_ADMIN)
    await screen.findByText('patient.created')

    expect(screen.getByLabelText(/^Search/)).toBeInTheDocument()
    expect(screen.queryByPlaceholderText('Search action or target…')).not.toBeInTheDocument()
    expect(screen.getAllByRole('textbox')).toHaveLength(2) // Search and Action
  })
})

describe('who can export', () => {
  it('shows no export control without audit.export', async () => {
    renderTab(HOSPITAL_ADMIN)

    await screen.findByText('patient.created')
    expect(screen.queryByRole('button', { name: /export/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /csv|json/i })).not.toBeInTheDocument()
  })

  it('offers both formats the API produces to a holder of audit.export', async () => {
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)

    expect(within(menu).getByRole('button', { name: /Download CSV/ })).toBeInTheDocument()
    expect(within(menu).getByRole('button', { name: /Download JSON/ })).toBeInTheDocument()
    expect(within(menu).getAllByRole('button')).toHaveLength(2)
    expect(within(menu).getByText('3 entries in the audit log.')).toBeInTheDocument()
    // Opening the menu asks for nothing.
    expect(exports()).toHaveLength(0)
  })
})

describe('exporting', () => {
  it('downloads the CSV the server built, under the name the server gave it', async () => {
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)

    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Downloaded audit-logs-20261005-101500.csv'))
    expect(exports()).toHaveLength(1)
    const [request] = exports()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/audit-logs/export')
    // No filters are set, so only the format is sent.
    expect(sentParams(request)).toEqual({ format: 'csv' })
    expect(request.responseType).toBe('blob')

    expect(downloads).toHaveLength(1)
    expect(downloads[0].filename).toBe('audit-logs-20261005-101500.csv')
    // The file is the server's bytes, not something rebuilt from the table.
    expect(await downloads[0].blob.text()).toBe(CSV)
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1)
    expect(toastError).not.toHaveBeenCalled()
  })

  it('asks for JSON when JSON is chosen', async () => {
    const body = JSON.stringify([entry('e1', 'patient.created')])
    onExport = () => file(body, 'application/json', 'audit-logs-20261005-101500.json')
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)

    await user.click(within(menu).getByRole('button', { name: /Download JSON/ }))

    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(sentParams(exports()[0])).toEqual({ format: 'json' })
    expect(downloads[0].filename).toBe('audit-logs-20261005-101500.json')
    expect(JSON.parse(await downloads[0].blob.text())).toHaveLength(1)
  })

  it('exports exactly the filters the table is showing', async () => {
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await screen.findByText('patient.created')

    fireEvent.change(screen.getByLabelText(/^Action/), { target: { value: ' patient.created ' } })
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2026-10-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2026-10-05' } })
    fireEvent.change(screen.getByPlaceholderText('user.invited…'), { target: { value: 'patient' } })
    // The search box is debounced; wait for the list to be asked with it.
    await waitFor(() => expect(sentParams(lists().at(-1)!)).toMatchObject({ q: 'patient' }))
    const listed = sentParams(lists().at(-1)!)

    const menu = await openMenu(user)
    expect(within(menu).getByText('3 entries match the current filters.')).toBeInTheDocument()
    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))
    await waitFor(() => expect(downloads).toHaveLength(1))

    const exported = sentParams(exports()[0])
    expect(exported).toEqual({
      format: 'csv',
      action: 'patient.created',
      q: 'patient',
      from: listed.from,
      to: listed.to,
    })
    // The dates are whole local days, sent as instants with an offset.
    expect(new Date(exported.from as string).getTime()).toBe(new Date(2026, 9, 1).getTime())
    expect(new Date(exported.to as string).getTime()).toBe(new Date(2026, 9, 6).getTime() - 1)
    // Paging belongs to the table, not to the file.
    expect(exported).not.toHaveProperty('page')
    expect(exported).not.toHaveProperty('page_size')
  })

  it('does not send a search term the server would reject as too short', async () => {
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await screen.findByText('patient.created')
    fireEvent.change(screen.getByPlaceholderText('user.invited…'), { target: { value: 'pa' } })

    const menu = await openMenu(user)
    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))

    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(sentParams(exports()[0])).toEqual({ format: 'csv' })
  })

  it('makes up a name of the same shape when the header cannot be read', async () => {
    onExport = () => file(CSV, 'text/csv')
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)

    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))

    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(downloads[0].filename).toMatch(/^audit-logs-\d{8}-\d{6}\.csv$/)
  })

  it('says when the file will hold only the newest 1,000 entries', async () => {
    total = 2500
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)

    expect(within(menu).getByText('2,500 entries in the audit log.')).toBeInTheDocument()
    expect(within(menu).getByRole('note')).toHaveTextContent('only the newest 1,000 are exported')
  })

  it('says when nothing matches, and still lets the empty file be fetched', async () => {
    rows = []
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await screen.findByText('No audit entries match')
    await user.click(screen.getByRole('button', { name: 'Export' }))
    const menu = await screen.findByRole('dialog', { name: 'Export audit log' })

    expect(within(menu).getByText(/the file will be empty/)).toBeInTheDocument()
    expect(within(menu).queryByRole('note')).not.toBeInTheDocument()
    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))
    await waitFor(() => expect(downloads).toHaveLength(1))
  })

  it('sends one request and shows progress while the file is being built', async () => {
    let release: () => void = () => {}
    onExport = () =>
      new Promise<Outcome>((resolve) => {
        release = () => resolve(file(CSV, 'text/csv', 'audit-logs-20261005-101500.csv'))
      })
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    const menu = await openMenu(user)
    const csv = within(menu).getByRole('button', { name: /Download CSV/ })
    const json = within(menu).getByRole('button', { name: /Download JSON/ })

    // Three clicks in the same tick, before React can close the menu.
    fireEvent.click(csv)
    fireEvent.click(csv)
    fireEvent.click(json)

    const trigger = await screen.findByRole('button', { name: 'Exporting…' })
    expect(trigger).toBeDisabled()
    expect(trigger).toHaveAttribute('aria-busy', 'true')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(exports()).toHaveLength(1)
    expect(downloads).toHaveLength(0)

    release()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(exports()).toHaveLength(1)
    expect(downloads).toHaveLength(1)
    expect(await screen.findByRole('button', { name: 'Export' })).toBeEnabled()
  })
})

describe('when the export fails', () => {
  async function exportCsv(user: ReturnType<typeof userEvent.setup>) {
    const menu = await openMenu(user)
    await user.click(within(menu).getByRole('button', { name: /Download CSV/ }))
  }

  /** As a browser delivers it: the JSON envelope, inside the blob that was asked for. */
  const blobFailure = (status: number, message: string): Outcome => ({
    status,
    data: new Blob([JSON.stringify({ success: false, message, error_code: 'VALIDATION_ERROR' })], {
      type: 'application/json',
    }),
  })

  it('422: shows the reason the server gave, and downloads nothing', async () => {
    onExport = () => blobFailure(422, 'Date range must not exceed one year per call.')
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await exportCsv(user)

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Date range must not exceed one year per call.'))
    expect(downloads).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
    // The table is untouched and the export can be tried again.
    expect(screen.getByText('patient.created')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export' })).toBeEnabled()
  })

  it('403: says the user may not export', async () => {
    onExport = () => blobFailure(403, 'Permission denied. Required: audit.export.')
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await exportCsv(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("You don't have permission to export the audit log."),
    )
    expect(downloads).toHaveLength(0)
  })

  it('500: hides the server text and lets the user retry', async () => {
    onExport = () => fail(500, 'Traceback: internal detail')
    const user = userEvent.setup()
    renderTab(SUPER_ADMIN)
    await exportCsv(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't export the audit log. Please try again."),
    )
    expect(downloads).toHaveLength(0)

    onExport = () => file(CSV, 'text/csv', 'audit-logs-20261005-101500.csv')
    await exportCsv(user)
    await waitFor(() => expect(downloads).toHaveLength(1))
    expect(exports()).toHaveLength(2)
  })
})
