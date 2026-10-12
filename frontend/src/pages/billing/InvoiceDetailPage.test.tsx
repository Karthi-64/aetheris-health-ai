import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import type { Invoice, Payment, Refund } from '@/api/billing'
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
  payment,
  refund,
  signIn,
  signOut,
  summaryOf,
} from '@/test/billingFixtures'
import InvoiceDetailPage from './InvoiceDetailPage'

/**
 * Invoice detail and every action on it, against docs/18-API_CONTRACTS.md §6.
 *
 * The real hooks and Axios instance run against an in-memory server. That
 * server does no arithmetic: each test states the invoice the API would
 * return (`after`), and the assertions check the page shows exactly that —
 * the same relationship the real screen has with the real API.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const inr = (value: string) => formatMoney(value, 'INR')

let api: FakeApi
let invoice: Invoice
let payments: Payment[]
let refunds: Refund[]
/** The invoice as the server returns it once the next write succeeds. */
let after: Partial<Invoice>
/** Override to refuse, or delay, the next write. */
let onWrite: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
/** Override the invoice read, e.g. to fail it. */
let onRead: (() => Outcome | Promise<Outcome>) | null

function write(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  Object.assign(invoice, after)
  if (url.endsWith('/payments')) {
    const body = bodyOf(config) as Partial<Payment>
    const recorded = payment({ id: `pay-${payments.length + 1}`, reference: null, ...body })
    payments.push(recorded)
    return ok({ payment: recorded, invoice: summaryOf(invoice) }, 201)
  }
  if (url.endsWith('/refund')) {
    const body = bodyOf(config) as Partial<Refund>
    const recorded = refund({ id: `ref-${refunds.length + 1}`, ...body })
    refunds.push(recorded)
    return ok({ refund: recorded, invoice: summaryOf(invoice) }, 201)
  }
  return ok({ ...invoice })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  invoice = draftInvoice()
  payments = []
  refunds = []
  after = {}
  onWrite = null
  onRead = null
  api = installFakeApi((config) => {
    const url = config.url ?? ''
    if (config.method !== 'get') return (onWrite ?? write)(config)
    if (url === '/invoices/inv-1') return onRead ? onRead() : ok({ ...invoice })
    if (url === '/invoices/inv-1/payments') return ok([...payments])
    if (url === '/invoices/inv-1/refunds') return ok([...refunds])
    if (url === '/services') return paged(SERVICES)
    if (url === '/appointments/appt-1') {
      return ok({
        id: 'appt-1',
        doctor_name: 'Priya Sharma',
        scheduled_start: '2026-10-02T04:00:00Z',
        status: 'completed',
      })
    }
    return fail(404, 'Not found.')
  })
})

afterEach(() => {
  api.restore()
  signOut()
})

function renderInvoice(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/billing/inv-1']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/billing/:invoiceId" element={<InvoiceDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

/** Wait for the invoice, then return the labels of the actions it offers. */
async function actions() {
  await screen.findByRole('region', { name: 'Totals' })
  const group = screen.queryByRole('group', { name: 'Invoice actions' })
  return group ? within(group).getAllByRole('button').map((b) => b.textContent?.trim()) : []
}

const action = (name: string | RegExp) =>
  within(screen.getByRole('group', { name: 'Invoice actions' })).getByRole('button', { name })

async function openAction(user: ReturnType<typeof userEvent.setup>, name: string) {
  await screen.findByRole('region', { name: 'Totals' })
  await user.click(action(name))
  return screen.findByRole('dialog')
}

const totals = () => within(screen.getByRole('region', { name: 'Totals' }))
const writes = () => api.sent.filter((c) => c.method !== 'get')
const keyOf = (config: InternalAxiosRequestConfig) => config.headers.get('Idempotency-Key') as string

async function setAmount(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, value: string) {
  const input = within(dialog).getByLabelText(/Amount/)
  await user.clear(input)
  if (value) await user.type(input, value)
}

describe('invoice detail', () => {
  it('shows the invoice exactly as the API returned it', async () => {
    invoice = issuedInvoice({ status: 'partially_paid', amount_paid: '200.00', balance_due: '400.00' })
    payments = [payment()]
    renderInvoice(ADMIN)

    expect(await screen.findByRole('heading', { name: 'INV-2026-000007' })).toBeInTheDocument()
    expect(screen.getByText('Partially paid')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Thomas George' })).toHaveAttribute('href', '/patients/pat-1')
    expect(screen.getByText('Counter items')).toBeInTheDocument()
    // The visit it bills.
    expect(await screen.findByText(/Priya Sharma/)).toBeInTheDocument()

    const charges = within(screen.getByRole('region', { name: 'Charges' }))
    const ecg = charges.getByRole('row', { name: /ECG \(12-lead\)/ })
    expect(within(ecg).getByText('1.00')).toBeInTheDocument()
    expect(within(ecg).getAllByText(inr('450.00'))).toHaveLength(2)
    const bandage = charges.getByRole('row', { name: /Crepe bandage/ })
    expect(within(bandage).getByText('2.00')).toBeInTheDocument()
    expect(within(bandage).getByText(inr('75.00'))).toBeInTheDocument()
    expect(within(bandage).getByText(inr('150.00'))).toBeInTheDocument()

    expect(totals().getByText('Subtotal').closest('div')).toHaveTextContent(inr('600.00'))
    expect(totals().getByText('Total').closest('div')).toHaveTextContent(inr('600.00'))
    expect(totals().getByText('Paid').closest('div')).toHaveTextContent(inr('200.00'))
    expect(totals().getByText('Outstanding').closest('div')).toHaveTextContent(inr('400.00'))

    const history = within(await screen.findByRole('region', { name: 'Payments' }))
    const paid = await history.findByRole('row', { name: /CARD-TXN-8899/ })
    expect(within(paid).getByText('Card')).toBeInTheDocument()
    expect(within(paid).getByText(inr('200.00'))).toBeInTheDocument()
  })

  it('shows a discount and its reason from the API', async () => {
    invoice = draftInvoice({
      discount_amount: '50.00',
      discount_reason: 'Staff family',
      total: '550.00',
      balance_due: '550.00',
    })
    renderInvoice(ADMIN)

    await screen.findByRole('region', { name: 'Totals' })
    const row = totals().getByText('Discount').closest('div')
    expect(row).toHaveTextContent(inr('50.00'))
    expect(row).toHaveTextContent('Staff family')
    expect(totals().getByText('Total').closest('div')).toHaveTextContent(inr('550.00'))
  })

  it('shows a loading state, then the invoice', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (finish = resolve))
    renderInvoice(ADMIN)

    expect(await screen.findByLabelText('Loading invoice')).toBeInTheDocument()
    finish(ok({ ...invoice }))
    expect(await screen.findByRole('heading', { name: 'Draft invoice' })).toBeInTheDocument()
  })

  it('says so when the invoice does not exist or is not the viewer\'s', async () => {
    onRead = () => fail(404, 'Invoice not found.')
    renderInvoice(DOCTOR)

    expect(await screen.findByText('Invoice not found')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Retry/ })).not.toBeInTheDocument()
  })

  it('offers a retry when the invoice fails to load', async () => {
    onRead = () => fail(500, 'Internal error.')
    const user = userEvent.setup()
    renderInvoice(ADMIN)

    expect(await screen.findByText("Couldn't load this invoice")).toBeInTheDocument()
    onRead = null
    await user.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await screen.findByRole('heading', { name: 'Draft invoice' })).toBeInTheDocument()
  })

  it('says when a draft has no charges yet', async () => {
    invoice = draftInvoice({ items: [], subtotal: '0.00', total: '0.00', balance_due: '0.00' })
    renderInvoice(ADMIN)

    expect(await screen.findByText('No charges have been added to this invoice yet.')).toBeInTheDocument()
    expect(action('Issue invoice')).toBeDisabled()
  })
})

describe('invoice actions by status and role', () => {
  it.each([
    ['a draft', () => draftInvoice(), ['Edit charges', 'Discount', 'Issue invoice']],
    [
      'a draft awaiting approval',
      () => draftInvoice({ discount_pending_approval: true }),
      ['Edit charges', 'Discount', 'Approve discount', 'Issue invoice'],
    ],
    ['an issued invoice', () => issuedInvoice(), ['Record payment', 'Void']],
    ['a part-paid invoice', () => issuedInvoice({ status: 'partially_paid' }), ['Record payment', 'Refund']],
    ['a paid invoice', () => issuedInvoice({ status: 'paid' }), ['Refund']],
    ['a void invoice', () => issuedInvoice({ status: 'void', void_reason: 'Raised in error' }), []],
    ['a refunded invoice', () => issuedInvoice({ status: 'refunded' }), []],
  ] as const)('gives an admin the right actions on %s', async (_name, make, expected) => {
    invoice = make()
    renderInvoice(ADMIN)
    expect(await actions()).toEqual(expected)
  })

  it('lets billing staff set a discount but not approve, void or refund', async () => {
    invoice = draftInvoice({ discount_pending_approval: true })
    renderInvoice(BILLING_STAFF)
    expect(await actions()).toEqual(['Edit charges', 'Discount', 'Issue invoice'])
  })

  it('gives billing staff payment only on an issued or paid invoice', async () => {
    invoice = issuedInvoice({ status: 'partially_paid' })
    renderInvoice(BILLING_STAFF)
    expect(await actions()).toEqual(['Record payment'])
  })

  it('gives a receptionist payment only, and nothing on a draft', async () => {
    invoice = issuedInvoice()
    renderInvoice(RECEPTIONIST)
    expect(await actions()).toEqual(['Record payment'])
  })

  it('gives a receptionist no actions on a draft', async () => {
    renderInvoice(RECEPTIONIST)
    expect(await actions()).toEqual([])
  })

  it('is read-only for a doctor', async () => {
    invoice = issuedInvoice({ status: 'partially_paid' })
    renderInvoice(DOCTOR)
    expect(await actions()).toEqual([])
  })

  it('blocks issuing while a discount awaits approval', async () => {
    invoice = draftInvoice({ discount_pending_approval: true, discount_amount: '200.00' })
    renderInvoice(ADMIN)

    expect(await screen.findByText('Discount awaiting approval')).toBeInTheDocument()
    expect(action('Issue invoice')).toBeDisabled()
  })
})

describe('issuing', () => {
  it('confirms, posts with no body, and shows the issued invoice', async () => {
    after = { status: 'issued', invoice_number: 'INV-2026-000007', issued_at: '2026-10-02T05:05:00Z' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Issue invoice')
    expect(within(dialog).getByText(/can't be edited/)).toBeInTheDocument()
    expect(writes()).toHaveLength(0)
    await user.click(within(dialog).getByRole('button', { name: 'Issue invoice' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Invoice INV-2026-000007 issued'))
    const [request] = writes()
    expect(request.method).toBe('post')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1/issue')
    expect(request.data).toBeUndefined()

    expect(await screen.findByRole('heading', { name: 'INV-2026-000007' })).toBeInTheDocument()
    expect(screen.getByText('Issued', { selector: 'span' })).toBeInTheDocument()
    expect(await actions()).toEqual(['Record payment'])
  })

  it('shows the API\'s reason when the draft cannot be issued', async () => {
    onWrite = () =>
      fail(400, 'An invoice needs at least one line before it can be issued.', {
        error_code: 'BUSINESS_RULE_VIOLATION',
      })
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Issue invoice')
    await user.click(within(dialog).getByRole('button', { name: 'Issue invoice' }))

    expect(
      await within(dialog).findByText('An invoice needs at least one line before it can be issued.'),
    ).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })
})

describe('recording a payment', () => {
  beforeEach(() => {
    invoice = issuedInvoice()
  })

  it('sends the payment with an Idempotency-Key and shows the new balance', async () => {
    after = { status: 'partially_paid', amount_paid: '200.00', balance_due: '400.00' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    // Starts at the server's outstanding balance.
    expect(within(dialog).getByLabelText(/Amount/)).toHaveValue('600.00')
    await setAmount(user, dialog, '200.00')
    await user.click(within(dialog).getByRole('combobox', { name: /Method/ }))
    await user.click(await screen.findByRole('option', { name: 'Card' }))
    await user.type(within(dialog).getByLabelText(/Reference/), 'CARD-TXN-8899')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Payment recorded'))
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1/payments')
    // A decimal string, never a number (§6.2).
    expect(bodyOf(request)).toEqual({ amount: '200.00', method: 'card', reference: 'CARD-TXN-8899' })
    expect(keyOf(request).length).toBeGreaterThanOrEqual(16)
    expect(keyOf(request).length).toBeLessThanOrEqual(100)

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByText('Partially paid')).toBeInTheDocument()
    expect(totals().getByText('Paid').closest('div')).toHaveTextContent(inr('200.00'))
    expect(totals().getByText('Outstanding').closest('div')).toHaveTextContent(inr('400.00'))
    const history = within(screen.getByRole('region', { name: 'Payments' }))
    expect(await history.findByRole('row', { name: /CARD-TXN-8899/ })).toBeInTheDocument()
  })

  it('reports an invoice paid in full', async () => {
    after = { status: 'paid', amount_paid: '600.00', balance_due: '0.00' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('Payment recorded — invoice paid in full'),
    )
    expect(await screen.findByText('Paid', { selector: 'span' })).toBeInTheDocument()
    expect(totals().getByText('Outstanding').closest('div')).toHaveTextContent(inr('0.00'))
    expect(await actions()).toEqual([])
  })

  it.each(['-50', '0', '12.345', 'abc', ''])('rejects the amount "%s" before any request', async (bad) => {
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await setAmount(user, dialog, bad)
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(await within(dialog).findByText(/Enter an amount above 0/)).toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })

  it('shows an overpayment refused by the API and refreshes the balance', async () => {
    // Another cashier took 500 while this dialog was open.
    onWrite = () => {
      Object.assign(invoice, { status: 'partially_paid', amount_paid: '500.00', balance_due: '100.00' })
      return fail(400, 'The payment exceeds the balance due of 100.00.', {
        error_code: 'BUSINESS_RULE_VIOLATION',
      })
    }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(
      await within(dialog).findByText('The payment exceeds the balance due of 100.00.'),
    ).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    // The page behind the dialog now shows what the server holds.
    await waitFor(() =>
      expect(
        within(screen.getByRole('region', { name: 'Totals', hidden: true }))
          .getByText('Outstanding')
          .closest('div'),
      ).toHaveTextContent(inr('100.00')),
    )
    // Still open, at the refreshed balance, ready to correct.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('shows the API\'s refusal when the invoice is already paid', async () => {
    onWrite = () => {
      Object.assign(invoice, { status: 'paid', amount_paid: '600.00', balance_due: '0.00' })
      return fail(400, 'A paid invoice cannot take a payment.', { error_code: 'BUSINESS_RULE_VIOLATION' })
    }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    // The refreshed invoice no longer takes payments, so the dialog closes
    // with it — the reason is still shown.
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('A paid invoice cannot take a payment.'),
    )
    expect(await screen.findByText('Paid', { selector: 'span' })).toBeInTheDocument()
    expect(await actions()).toEqual([])
  })

  it('shows an amount error from the API under the field', async () => {
    onWrite = () =>
      fail(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: [{ field: 'amount', message: 'Input should be greater than 0' }],
      })
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))

    expect(await within(dialog).findByText('Input should be greater than 0')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('offers a receptionist cash only, and shows a refusal from the API', async () => {
    onWrite = () =>
      fail(403, 'Your role can record cash payments only.', { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    renderInvoice(RECEPTIONIST)

    const dialog = await openAction(user, 'Record payment')
    expect(within(dialog).getByText(/cash payments only/)).toBeInTheDocument()
    await user.click(within(dialog).getByRole('combobox', { name: /Method/ }))
    expect((await screen.findAllByRole('option')).map((o) => o.textContent)).toEqual(['Cash'])
    await user.keyboard('{Escape}')

    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
    expect(bodyOf(writes()[0])).toMatchObject({ method: 'cash' })
    expect(await within(dialog).findByText('Your role can record cash payments only.')).toBeInTheDocument()
  })

  it('sends one request while a payment is in flight', async () => {
    after = { status: 'paid', amount_paid: '600.00', balance_due: '0.00' }
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(write(config))))
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await within(dialog).findByRole('button', { name: /Recording/ })
    expect(busy).toBeDisabled()
    expect(writes()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(writes()).toHaveLength(1)
    expect(payments).toHaveLength(1)
  })

  it('retries a failed payment with the same key, without showing internal errors', async () => {
    after = { status: 'paid', amount_paid: '600.00', balance_due: '0.00' }
    onWrite = () => fail(500, 'Traceback (most recent call last): asyncpg…')
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Record payment')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
    expect(await within(dialog).findByText(/Couldn't record the payment/)).toBeInTheDocument()
    expect(within(dialog).queryByText(/Traceback/)).not.toBeInTheDocument()

    onWrite = null
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalled())

    expect(writes()).toHaveLength(2)
    expect(keyOf(writes()[1])).toBe(keyOf(writes()[0]))
  })

  it('uses a new key for a second payment on the same invoice', async () => {
    after = { status: 'partially_paid', amount_paid: '300.00', balance_due: '300.00' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    let dialog = await openAction(user, 'Record payment')
    await setAmount(user, dialog, '300.00')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    after = { status: 'paid', amount_paid: '600.00', balance_due: '0.00' }
    dialog = await openAction(user, 'Record payment')
    // Reopened at the new outstanding balance — the same amount as before.
    expect(within(dialog).getByLabelText(/Amount/)).toHaveValue('300.00')
    await user.click(within(dialog).getByRole('button', { name: 'Record payment' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(2))

    expect(bodyOf(writes()[1])).toEqual(bodyOf(writes()[0]))
    expect(keyOf(writes()[1])).not.toBe(keyOf(writes()[0]))
  })
})

describe('discounts', () => {
  it('sends the discount and shows it as awaiting approval when the API says so', async () => {
    after = {
      discount_amount: '200.00',
      discount_reason: 'Financial hardship',
      discount_pending_approval: true,
      total: '400.00',
      balance_due: '400.00',
    }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Discount')
    const amount = within(dialog).getByLabelText(/Discount amount/)
    await user.clear(amount)
    await user.type(amount, '200.00')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Financial hardship')
    await user.click(within(dialog).getByRole('button', { name: 'Save discount' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith(
        'Discount saved — it needs approval before the invoice can be issued',
      ),
    )
    const [request] = writes()
    expect(request.method).toBe('patch')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1')
    // Only the discount: no total, and no approval flag the client could set.
    expect(bodyOf(request)).toEqual({ discount_amount: '200.00', discount_reason: 'Financial hardship' })

    expect(await screen.findByText('Discount awaiting approval')).toBeInTheDocument()
    expect(totals().getByText('Total').closest('div')).toHaveTextContent(inr('400.00'))
    expect(action('Issue invoice')).toBeDisabled()
    // Billing Staff cannot approve their own discount.
    expect(await actions()).not.toContain('Approve discount')
  })

  it('applies a small discount without approval', async () => {
    after = { discount_amount: '20.00', discount_reason: 'Rounding', total: '580.00', balance_due: '580.00' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Discount')
    const amount = within(dialog).getByLabelText(/Discount amount/)
    await user.clear(amount)
    await user.type(amount, '20')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Rounding')
    await user.click(within(dialog).getByRole('button', { name: 'Save discount' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Discount applied'))
    expect(action('Issue invoice')).toBeEnabled()
  })

  it('requires a reason for a discount above zero', async () => {
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Discount')
    const amount = within(dialog).getByLabelText(/Discount amount/)
    await user.clear(amount)
    await user.type(amount, '50')
    await user.click(within(dialog).getByRole('button', { name: 'Save discount' }))

    expect(await within(dialog).findByText('Give a reason for the discount')).toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })

  it('shows the API\'s error when the discount exceeds the subtotal', async () => {
    onWrite = () =>
      fail(422, 'The discount cannot exceed the subtotal.', {
        error_code: 'VALIDATION_ERROR',
        errors: { errors: [{ field: 'discount_amount', message: 'The discount cannot exceed the subtotal.' }] },
      })
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Discount')
    const amount = within(dialog).getByLabelText(/Discount amount/)
    await user.clear(amount)
    await user.type(amount, '9999')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Goodwill')
    await user.click(within(dialog).getByRole('button', { name: 'Save discount' }))

    expect(await within(dialog).findByText('The discount cannot exceed the subtotal.')).toBeInTheDocument()
  })

  it('removes a discount by sending zero', async () => {
    invoice = draftInvoice({ discount_amount: '50.00', discount_reason: 'Staff family', total: '550.00' })
    after = { discount_amount: '0.00', discount_reason: null, total: '600.00' }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Discount')
    const amount = within(dialog).getByLabelText(/Discount amount/)
    expect(amount).toHaveValue('50.00')
    await user.clear(amount)
    await user.type(amount, '0')
    await user.click(within(dialog).getByRole('button', { name: 'Save discount' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Discount removed'))
    expect(bodyOf(writes()[0])).toEqual({ discount_amount: '0' })
  })

  it('lets an admin approve a pending discount, which unblocks issuing', async () => {
    invoice = draftInvoice({ discount_amount: '200.00', discount_pending_approval: true, total: '400.00' })
    after = { discount_pending_approval: false, discount_approved_by: 'u1' }
    const user = userEvent.setup()
    renderInvoice(ADMIN)
    await screen.findByText('Discount awaiting approval')

    await user.click(action('Approve discount'))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Discount approved'))
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1/approve-discount')
    expect(request.data).toBeUndefined()

    await waitFor(() => expect(screen.queryByText('Discount awaiting approval')).not.toBeInTheDocument())
    expect(action('Issue invoice')).toBeEnabled()
    expect(await actions()).not.toContain('Approve discount')
  })

  it('handles a discount someone else already approved (409)', async () => {
    invoice = draftInvoice({ discount_amount: '200.00', discount_pending_approval: true })
    onWrite = () => {
      invoice.discount_pending_approval = false
      return fail(409, 'This invoice has no discount awaiting approval.', { error_code: 'RESOURCE_CONFLICT' })
    }
    const user = userEvent.setup()
    renderInvoice(ADMIN)
    await screen.findByText('Discount awaiting approval')

    await user.click(action('Approve discount'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('This invoice has no discount awaiting approval.'),
    )
    // Refetched: the page no longer claims it is pending.
    await waitFor(() => expect(screen.queryByText('Discount awaiting approval')).not.toBeInTheDocument())
  })
})

describe('refunds', () => {
  beforeEach(() => {
    invoice = issuedInvoice({ status: 'paid', amount_paid: '600.00', balance_due: '0.00' })
    payments = [payment({ amount: '600.00' })]
  })

  it('confirms with a reason, sends an Idempotency-Key, and shows the refund', async () => {
    after = { status: 'refunded', amount_refunded: '600.00' }
    const user = userEvent.setup()
    renderInvoice(ADMIN)

    const dialog = await openAction(user, 'Refund')
    expect(within(dialog).getByText(/can't be undone/)).toBeInTheDocument()

    // A refund needs an amount and a reason before anything is sent.
    await user.click(within(dialog).getByRole('button', { name: 'Issue refund' }))
    expect(await within(dialog).findByText('Give a reason for the refund')).toBeInTheDocument()
    expect(within(dialog).getByText(/Enter an amount above 0/)).toBeInTheDocument()
    expect(writes()).toHaveLength(0)

    await setAmount(user, dialog, '600.00')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Sample could not be processed')
    await user.click(within(dialog).getByRole('button', { name: 'Issue refund' }))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('Refund issued — invoice closed as refunded'),
    )
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1/refund')
    expect(bodyOf(request)).toEqual({
      amount: '600.00',
      method: 'cash',
      reason: 'Sample could not be processed',
    })
    expect(keyOf(request).length).toBeGreaterThanOrEqual(16)

    expect(await screen.findByText('Refunded', { selector: 'span' })).toBeInTheDocument()
    expect(totals().getByText('Refunded').closest('div')).toHaveTextContent(inr('600.00'))
    const history = within(await screen.findByRole('region', { name: 'Refunds' }))
    expect(await history.findByRole('row', { name: /Sample could not be processed/ })).toBeInTheDocument()
    expect(await actions()).toEqual([])
  })

  it('shows the API\'s refusal when the refund exceeds what was paid', async () => {
    onWrite = () =>
      fail(400, 'The refund exceeds the 600.00 still refundable.', { error_code: 'BUSINESS_RULE_VIOLATION' })
    const user = userEvent.setup()
    renderInvoice(ADMIN)

    const dialog = await openAction(user, 'Refund')
    await setAmount(user, dialog, '900')
    await user.type(within(dialog).getByLabelText(/Reason/), 'Goodwill')
    await user.click(within(dialog).getByRole('button', { name: 'Issue refund' }))

    expect(await within(dialog).findByText('The refund exceeds the 600.00 still refundable.')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('does nothing when the refund is dismissed', async () => {
    const user = userEvent.setup()
    renderInvoice(ADMIN)

    await openAction(user, 'Refund')
    await user.click(screen.getByRole('button', { name: 'Keep as is' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(writes()).toHaveLength(0)
  })
})

describe('voiding and editing', () => {
  it('voids an issued invoice with a reason', async () => {
    invoice = issuedInvoice()
    after = { status: 'void', void_reason: 'Raised against the wrong patient', voided_at: '2026-10-02T06:00:00Z' }
    const user = userEvent.setup()
    renderInvoice(ADMIN)

    const dialog = await openAction(user, 'Void')
    await user.click(within(dialog).getByRole('button', { name: 'Void invoice' }))
    expect(await within(dialog).findByText('Give a reason for voiding')).toBeInTheDocument()
    expect(writes()).toHaveLength(0)

    await user.type(within(dialog).getByLabelText(/Reason/), 'Raised against the wrong patient')
    await user.click(within(dialog).getByRole('button', { name: 'Void invoice' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Invoice voided'))
    const [request] = writes()
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/invoices/inv-1/void')
    expect(bodyOf(request)).toEqual({ reason: 'Raised against the wrong patient' })

    expect(await screen.findByText('This invoice was voided')).toBeInTheDocument()
    expect(screen.getByText('Raised against the wrong patient')).toBeInTheDocument()
    expect(await actions()).toEqual([])
  })

  it('edits a draft\'s charges by replacing the whole line set', async () => {
    after = {
      items: [draftInvoice().items[0]],
      subtotal: '450.00',
      total: '450.00',
      balance_due: '450.00',
    }
    const user = userEvent.setup()
    renderInvoice(BILLING_STAFF)

    const dialog = await openAction(user, 'Edit charges')
    // Opens with the lines the server holds.
    const second = within(dialog).getByRole('listitem', { name: 'Item 2' })
    expect(within(second).getByLabelText(/Description/)).toHaveValue('Crepe bandage')
    expect(within(second).getByLabelText(/Unit price/)).toHaveValue('75.00')

    await user.click(within(dialog).getByRole('button', { name: 'Remove item 2' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save charges' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Charges updated'))
    const [request] = writes()
    expect(request.method).toBe('patch')
    // Every remaining line is sent; the catalog line carries no price.
    expect(bodyOf(request)).toEqual({ items: [{ service_id: 'svc-ecg', quantity: '1.00' }] })

    await waitFor(() =>
      expect(totals().getByText('Total').closest('div')).toHaveTextContent(inr('450.00')),
    )
    expect(screen.queryByRole('row', { name: /Crepe bandage/ })).not.toBeInTheDocument()
  })
})
