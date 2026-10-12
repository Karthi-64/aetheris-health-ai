import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InvoiceSummary } from '@/api/billing'
import { formatMoney } from '@/lib/format'
import { fail, installFakeApi, paged, type FakeApi } from '@/test/fakeApi'
import { DOCTOR, RECEPTIONIST, issuedInvoice, signIn, signOut, summaryOf } from '@/test/billingFixtures'
import { PatientInvoices } from './PatientInvoices'

/** The billing section on a patient's record: patient → invoices → Billing. */

let api: FakeApi
let invoices: InvoiceSummary[]
let total: number | undefined

beforeEach(() => {
  invoices = [
    summaryOf(issuedInvoice({ status: 'partially_paid', amount_paid: '200.00', balance_due: '400.00' })),
  ]
  total = undefined
  api = installFakeApi((config) =>
    config.url === '/invoices' ? paged(invoices, total) : fail(404, 'Not found.'),
  )
})

afterEach(() => {
  api.restore()
  signOut()
})

function renderSection(permissions: string[]) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <PatientInvoices patientId="pat-1" />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('PatientInvoices', () => {
  it("lists the patient's invoices and links each into Billing", async () => {
    renderSection(RECEPTIONIST)

    const link = await screen.findByRole('link', { name: /INV-2026-000007/ })
    expect(link).toHaveAttribute('href', '/billing/inv-1')
    expect(link).toHaveTextContent('Partially paid')
    expect(link).toHaveTextContent(formatMoney('600.00', 'INR'))
    expect(link).toHaveTextContent(formatMoney('400.00', 'INR'))
    expect(api.requests('get', '/invoices')[0].params).toMatchObject({ patient_id: 'pat-1' })
    expect(screen.getByRole('link', { name: /Open in Billing/ })).toHaveAttribute(
      'href',
      '/billing?patient_id=pat-1',
    )
  })

  it('links to the full list when there are more than it shows', async () => {
    total = 12
    renderSection(RECEPTIONIST)

    expect(await screen.findByRole('link', { name: /View all 12 invoices/ })).toBeInTheDocument()
  })

  it('says when the patient has no invoices', async () => {
    invoices = []
    renderSection(RECEPTIONIST)

    expect(await screen.findByText('No invoices for this patient yet.')).toBeInTheDocument()
  })

  it('shows a doctor the invoices the server returns for them', async () => {
    renderSection(DOCTOR)
    expect(await screen.findByRole('link', { name: /INV-2026-000007/ })).toBeInTheDocument()
  })

  it('renders nothing, and asks for nothing, without an invoice permission', async () => {
    renderSection(['patient.read'])

    expect(screen.queryByRole('region', { name: 'Billing' })).not.toBeInTheDocument()
    await waitFor(() => expect(api.sent).toHaveLength(0))
  })
})
