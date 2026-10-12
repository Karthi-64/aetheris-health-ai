import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InvoiceSummary } from '@/api/billing'
import { formatMoney } from '@/lib/format'
import { bodyOf, fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import {
  ADMIN,
  BILLING_STAFF,
  DOCTOR,
  RECEPTIONIST,
  SERVICES,
  draftInvoice,
  issuedInvoice,
  signIn,
  signOut,
  summaryOf,
} from '@/test/billingFixtures'
import BillingPage from './BillingPage'

/**
 * The invoice list and "New invoice", against docs/18-API_CONTRACTS.md §6.4.
 * The real hooks and Axios instance run against an in-memory server.
 */

const { toastSuccess } = vi.hoisted(() => ({ toastSuccess: vi.fn() }))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: vi.fn() } }))

const PARTIAL = summaryOf(
  issuedInvoice({
    id: 'inv-2',
    invoice_number: 'INV-2026-000003',
    status: 'partially_paid',
    total: '750.00',
    amount_paid: '300.00',
    balance_due: '450.00',
  }),
)
const DRAFT_PENDING: InvoiceSummary = {
  ...summaryOf(draftInvoice({ id: 'inv-3', patient_id: 'pat-2', patient_name: 'Riya Fernandes' })),
  discount_pending_approval: true,
}

let api: FakeApi
let invoices: InvoiceSummary[]
let onInvoices: (() => Outcome | Promise<Outcome>) | null
let onCreate: (() => Outcome) | null

beforeEach(() => {
  toastSuccess.mockReset()
  invoices = [PARTIAL, DRAFT_PENDING]
  onInvoices = null
  onCreate = null
  api = installFakeApi((config) => {
    const url = config.url ?? ''
    if (config.method === 'post' && url === '/invoices') {
      return onCreate ? onCreate() : ok(draftInvoice({ id: 'inv-new' }), 201)
    }
    if (url === '/invoices') return onInvoices ? onInvoices() : paged(invoices)
    if (url === '/invoices/inv-new') return ok(draftInvoice({ id: 'inv-new' }))
    if (url === '/services') return paged(SERVICES)
    if (url === '/patients') {
      return paged(
        config.params?.q ? [{ id: 'pat-1', full_name: 'Thomas George', mrn: 'MRN-2026-00004' }] : [],
      )
    }
    if (url === '/patients/pat-1') {
      return ok({ id: 'pat-1', full_name: 'Thomas George', mrn: 'MRN-2026-00004' })
    }
    if (url === '/appointments') {
      return paged([
        {
          id: 'appt-1',
          patient_id: 'pat-1',
          patient_name: 'Thomas George',
          doctor_id: 'd1',
          doctor_name: 'Priya Sharma',
          scheduled_start: '2026-10-02T04:00:00Z',
          scheduled_end: '2026-10-02T04:30:00Z',
          status: 'completed',
          type: 'new',
        },
      ])
    }
    return fail(404, 'Not found.')
  })
})

afterEach(() => {
  api.restore()
  signOut()
})

function renderBilling(permissions: string[], path = '/billing') {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[path]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/billing" element={<BillingPage />} />
          <Route path="/billing/:invoiceId" element={<p>Invoice detail page</p>} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const listRequests = () => api.requests('get').filter((c) => c.url === '/invoices')
const lastListParams = () => listRequests().at(-1)?.params as Record<string, unknown>

describe('BillingPage', () => {
  it('lists invoices with the status and amounts the API returned', async () => {
    renderBilling(BILLING_STAFF)

    const row = await screen.findByRole('row', { name: /Thomas George/ })
    expect(within(row).getByText('INV-2026-000003')).toBeInTheDocument()
    expect(within(row).getByText('Partially paid')).toBeInTheDocument()
    expect(within(row).getByText(formatMoney('750.00', 'INR'))).toBeInTheDocument()
    expect(within(row).getByText(formatMoney('300.00', 'INR'))).toBeInTheDocument()
    expect(within(row).getByText(formatMoney('450.00', 'INR'))).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: /View invoice for Thomas George/ })).toHaveAttribute(
      'href',
      '/billing/inv-2',
    )

    // A draft has no number yet, and one waiting on a discount says so.
    const draft = screen.getByRole('row', { name: /Riya Fernandes/ })
    expect(within(draft).getByText('Not yet issued')).toBeInTheDocument()
    expect(within(draft).getByText('Draft')).toBeInTheDocument()
    expect(within(draft).getByText('Awaiting approval')).toBeInTheDocument()

    expect(lastListParams()).toMatchObject({ page: 1, page_size: 25 })
  })

  it('shows loading placeholders until the list arrives', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onInvoices = () => new Promise<Outcome>((resolve) => (finish = resolve))
    renderBilling(BILLING_STAFF)

    await waitFor(() => expect(listRequests()).toHaveLength(1))
    expect(screen.queryByText('No invoices yet')).not.toBeInTheDocument()
    expect(screen.queryByRole('row', { name: /Thomas George/ })).not.toBeInTheDocument()
    // Header row plus skeleton rows.
    expect(screen.getAllByRole('row').length).toBeGreaterThan(1)

    finish(paged(invoices))
    expect(await screen.findByRole('row', { name: /Thomas George/ })).toBeInTheDocument()
  })

  it('shows an empty state when there are no invoices', async () => {
    invoices = []
    renderBilling(BILLING_STAFF)

    expect(await screen.findByText('No invoices yet')).toBeInTheDocument()
  })

  it('shows an error with a working retry when the list fails', async () => {
    onInvoices = () => fail(500, 'Internal error.')
    const user = userEvent.setup()
    renderBilling(BILLING_STAFF)

    expect(await screen.findByText("Couldn't load invoices")).toBeInTheDocument()

    onInvoices = null
    await user.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByRole('row', { name: /Thomas George/ })).toBeInTheDocument()
  })

  it('filters by status using the query name the API reads', async () => {
    const user = userEvent.setup()
    renderBilling(BILLING_STAFF)
    await screen.findByRole('row', { name: /Thomas George/ })

    await user.click(screen.getByRole('combobox', { name: 'Filter invoices' }))
    await user.click(await screen.findByRole('option', { name: 'Paid' }))

    await waitFor(() => expect(lastListParams()).toMatchObject({ status: 'paid' }))
    expect(lastListParams().discount_pending).toBeUndefined()
  })

  it('offers the discount approval queue as a filter', async () => {
    const user = userEvent.setup()
    renderBilling(ADMIN)
    await screen.findByRole('row', { name: /Thomas George/ })

    await user.click(screen.getByRole('combobox', { name: 'Filter invoices' }))
    await user.click(await screen.findByRole('option', { name: 'Awaiting approval' }))

    await waitFor(() => expect(lastListParams()).toMatchObject({ discount_pending: true }))
    expect(lastListParams().status).toBeUndefined()
  })

  it('narrows to one patient from the URL and can widen again', async () => {
    invoices = [PARTIAL]
    const user = userEvent.setup()
    renderBilling(BILLING_STAFF, '/billing?patient_id=pat-1')

    expect(await screen.findByText(/Showing invoices for/)).toBeInTheDocument()
    expect(lastListParams()).toMatchObject({ patient_id: 'pat-1' })
    expect(await screen.findByRole('link', { name: 'Thomas George' })).toHaveAttribute(
      'href',
      '/patients/pat-1',
    )

    await user.click(screen.getByRole('button', { name: /Show all patients/ }))
    await waitFor(() => expect(lastListParams().patient_id).toBeUndefined())
  })

  it('hides New invoice from roles that cannot create one', async () => {
    renderBilling(RECEPTIONIST)
    await screen.findByRole('row', { name: /Thomas George/ })
    expect(screen.queryByRole('button', { name: /New invoice/ })).not.toBeInTheDocument()
  })

  it('renders the list read-only for a doctor', async () => {
    renderBilling(DOCTOR)
    expect(await screen.findByRole('row', { name: /Thomas George/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /New invoice/ })).not.toBeInTheDocument()
  })
})

describe('New invoice', () => {
  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    await screen.findByRole('row', { name: /Thomas George/ })
    await user.click(screen.getByRole('button', { name: /New invoice/ }))
    return screen.findByRole('dialog')
  }

  async function choosePatient(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement) {
    await user.type(within(dialog).getByLabelText(/Patient/), 'Thom')
    await user.click(await within(dialog).findByRole('button', { name: /Thomas George/ }))
  }

  async function chooseService(
    user: ReturnType<typeof userEvent.setup>,
    item: HTMLElement,
    option: RegExp,
  ) {
    await user.click(within(item).getByRole('combobox'))
    await user.click(await screen.findByRole('option', { name: option }))
  }

  it('creates a draft from a catalog line and a custom line, then opens it', async () => {
    const user = userEvent.setup()
    renderBilling(ADMIN)
    const dialog = await openDialog(user)
    await choosePatient(user, dialog)

    await user.click(within(dialog).getByRole('combobox', { name: /Appointment/ }))
    await user.click(await screen.findByRole('option', { name: /Priya Sharma/ }))

    await chooseService(user, within(dialog).getByRole('listitem', { name: 'Item 1' }), /ECG \(12-lead\)/)

    await user.click(within(dialog).getByRole('button', { name: /Add item/ }))
    const second = within(dialog).getByRole('listitem', { name: 'Item 2' })
    await chooseService(user, second, /Custom item/)
    await user.type(within(second).getByLabelText(/Description/), 'Crepe bandage')
    await user.clear(within(second).getByLabelText(/Quantity/))
    await user.type(within(second).getByLabelText(/Quantity/), '2')
    await user.type(within(second).getByLabelText(/Unit price/), '75.00')

    await user.type(within(dialog).getByLabelText(/Notes/), 'Counter items')
    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByText('Invoice detail page')).toBeInTheDocument()
    expect(toastSuccess).toHaveBeenCalledWith('Draft invoice created')

    const [request] = api.requests('post', '/invoices')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices')
    // No totals, and no price or tax flag on the catalog line: the API rejects them.
    expect(bodyOf(request)).toEqual({
      patient_id: 'pat-1',
      appointment_id: 'appt-1',
      items: [
        { service_id: 'svc-ecg', quantity: '1' },
        { description: 'Crepe bandage', quantity: '2', unit_price: '75.00', taxable: false },
      ],
      notes: 'Counter items',
    })
  })

  it('blocks an incomplete invoice before any request', async () => {
    const user = userEvent.setup()
    renderBilling(ADMIN)
    const dialog = await openDialog(user)

    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    expect(await within(dialog).findByText('Choose a patient')).toBeInTheDocument()
    expect(within(dialog).getByText('Choose a service or a custom item')).toBeInTheDocument()
    expect(api.requests('post', '/invoices')).toHaveLength(0)
  })

  it('validates a custom line', async () => {
    const user = userEvent.setup()
    renderBilling(ADMIN)
    const dialog = await openDialog(user)
    await choosePatient(user, dialog)
    const item = within(dialog).getByRole('listitem', { name: 'Item 1' })
    await chooseService(user, item, /Custom item/)
    await user.clear(within(item).getByLabelText(/Quantity/))
    await user.type(within(item).getByLabelText(/Quantity/), '0')
    await user.type(within(item).getByLabelText(/Unit price/), '-5')

    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    expect(await within(item).findByText('Describe the item')).toBeInTheDocument()
    expect(within(item).getByText(/Enter a quantity above 0/)).toBeInTheDocument()
    expect(within(item).getByText(/Enter a price/)).toBeInTheDocument()
    expect(api.requests('post', '/invoices')).toHaveLength(0)
  })

  it('shows a line error from the API on that line', async () => {
    onCreate = () =>
      fail(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: { errors: [{ field: 'items.0.service_id', message: 'This service is no longer active.' }] },
      })
    const user = userEvent.setup()
    renderBilling(ADMIN)
    const dialog = await openDialog(user)
    await choosePatient(user, dialog)
    const item = within(dialog).getByRole('listitem', { name: 'Item 1' })
    await chooseService(user, item, /ECG \(12-lead\)/)

    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    expect(await within(item).findByText('This service is no longer active.')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('explains when the appointment already has an invoice (409)', async () => {
    onCreate = () =>
      fail(409, 'This appointment already has an invoice. Edit or void that one instead.', {
        error_code: 'RESOURCE_CONFLICT',
      })
    const user = userEvent.setup()
    renderBilling(ADMIN)
    const dialog = await openDialog(user)
    await choosePatient(user, dialog)
    await chooseService(user, within(dialog).getByRole('listitem', { name: 'Item 1' }), /ECG \(12-lead\)/)

    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    expect(
      await within(dialog).findByText('This appointment already has an invoice. Edit or void that one instead.'),
    ).toBeInTheDocument()
  })

  it('starts with the patient chosen when raised from a patient filter', async () => {
    invoices = [PARTIAL]
    const user = userEvent.setup()
    renderBilling(BILLING_STAFF, '/billing?patient_id=pat-1')
    await screen.findByRole('link', { name: 'Thomas George' })

    await user.click(screen.getByRole('button', { name: /New invoice/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/MRN-2026-00004/)).toBeInTheDocument()
    // Billing Staff cannot read appointments, so no appointment picker is offered.
    expect(within(dialog).queryByRole('combobox', { name: /Appointment/ })).not.toBeInTheDocument()

    await chooseService(user, within(dialog).getByRole('listitem', { name: 'Item 1' }), /Complete blood count/)
    await user.click(within(dialog).getByRole('button', { name: 'Create draft' }))

    await screen.findByText('Invoice detail page')
    expect(bodyOf(api.requests('post', '/invoices')[0])).toEqual({
      patient_id: 'pat-1',
      items: [{ service_id: 'svc-cbc', quantity: '1' }],
    })
  })
})
