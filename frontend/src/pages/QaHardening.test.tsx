import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, matchRoutes, type RouteObject } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { LabOrder } from '@/api/lab'
import type { Prescription } from '@/api/pharmacy'
import Breadcrumbs from '@/components/layout/Breadcrumbs'
import { DataTable } from '@/components/ui/data-table'
import { Detail, InfoCard } from '@/components/ui/detail-card'
import DashboardLayout from '@/layouts/DashboardLayout'
import { formatDateTime } from '@/lib/format'
import { router } from '@/router'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, ok, paged, type FakeApi } from '@/test/fakeApi'
import {
  adminDashboardFixture,
  billingDashboardFixture,
  doctorDashboardFixture,
  receptionDashboardFixture,
} from '@/test/reportFixtures'
import DashboardPage from './DashboardPage'
import DoctorsPage from './doctors/DoctorsPage'
import { labOrderColumns } from './laboratory/columns'
import PatientsPage from './patients/PatientsPage'
import { prescriptionColumns } from './pharmacy/prescriptionColumns'
import SettingsPage from './settings/SettingsPage'

/** Fixes from the final demo QA pass, each held in place by a test. */

let fake: FakeApi

/** What each dashboard route answers, for the roles that now read one. */
const DASHBOARDS: Record<string, unknown> = {
  '/dashboards/admin': adminDashboardFixture,
  '/dashboards/billing': billingDashboardFixture,
  '/dashboards/reception': receptionDashboardFixture,
  '/dashboards/doctor': doctorDashboardFixture,
}

beforeEach(() => {
  fake = installFakeApi((config) => {
    if (config.url === '/notifications/unread-count') return ok({ unread: 0 })
    const dashboard = DASHBOARDS[config.url ?? '']
    return dashboard ? ok(dashboard) : paged([])
  })
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderIn(page: React.ReactNode, permissions: string[], shell = false) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/patients']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route element={shell ? <DashboardLayout /> : undefined}>
            <Route path="/patients" element={page} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('detail cards', () => {
  it('let a long unbroken value wrap instead of widening the page', () => {
    // A doctor's email overflowed its column and scrolled the page sideways.
    render(
      <InfoCard title="Practice">
        <Detail label="Email" value="a.very.long.address@demohospital.example.com" />
      </InfoCard>,
    )

    const value = screen.getByText('a.very.long.address@demohospital.example.com')
    expect(value).toHaveClass('[overflow-wrap:anywhere]')
    // The grid column must be allowed to shrink below the value's width.
    expect(value.parentElement).toHaveClass('min-w-0')
  })
})

describe('patient search', () => {
  it('explains how search matches when a full name finds nothing', async () => {
    // The API matches the start of a first or last name, or a whole MRN or
    // phone number — "Ravi Menon" as one term matches no one.
    const user = userEvent.setup()
    renderIn(<PatientsPage />, ['patient.read', 'patient.create'])

    await user.type(await screen.findByLabelText('Search name, MRN or phone…'), 'Ravi Menon')

    expect(await screen.findByText('No matching patients')).toBeInTheDocument()
    expect(screen.getByText(/a full name will not match/)).toBeInTheDocument()
    expect(screen.getByText(/MRN or phone number must be entered in full/)).toBeInTheDocument()
  })

  it('offers Register on an empty registry only to those who may register', async () => {
    renderIn(<PatientsPage />, ['patient.read'])

    const empty = (await screen.findByText('No patients yet')).closest('div') as HTMLElement
    expect(screen.getByText('Patients registered at this hospital will appear here.')).toBeInTheDocument()
    expect(within(empty).queryByRole('button', { name: /Register/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Register Patient/ })).not.toBeInTheDocument()
  })

  it('still offers Register on an empty registry to a user with patient.create', async () => {
    renderIn(<PatientsPage />, ['patient.read', 'patient.create'])

    await screen.findByText('No patients yet')
    expect(screen.getByText('Register your first patient to start building the registry.')).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /Register Patient/ }).length).toBeGreaterThan(0)
  })
})

describe('top bar', () => {
  it('has no search box, and nothing in the shell says "coming soon"', async () => {
    // There is no search to offer, so the bar does not show a disabled one.
    renderIn(<p>Page body</p>, ['patient.read'], true)
    await screen.findByText('Page body')

    const bar = screen.getByRole('banner')
    expect(within(bar).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(bar).queryByRole('searchbox')).not.toBeInTheDocument()
    // Text, placeholders, titles and labels alike.
    expect(document.body.innerHTML).not.toMatch(/coming soon/i)
    expect(document.body.innerHTML).not.toMatch(/global search/i)
  })

  it('keeps the breadcrumbs on the left and the actions against the right edge', async () => {
    renderIn(<p>Page body</p>, ['patient.read', 'notification.read.own'], true)
    await screen.findByText('Page body')

    const bar = screen.getByRole('banner')
    expect(within(bar).getByRole('navigation', { name: 'Breadcrumb' })).toBeInTheDocument()
    expect(within(bar).getByRole('button', { name: 'Open menu' })).toBeInTheDocument()
    // With the search box gone nothing else pushes the actions right.
    const actions = bar.lastElementChild as HTMLElement
    expect(actions).toHaveClass('ml-auto')
    expect(await within(actions).findByRole('button', { name: /^Notifications/ })).toBeInTheDocument()
    expect(within(actions).getByText('Test User')).toBeInTheDocument()
  })
})

describe('breadcrumbs', () => {
  const ID = '3f2b8c1e-7a4d-4e9b-9c55-0d1e2f3a4b5c'

  /** Every path the real router mounts, with a record id in place of each parameter. */
  function mountedPaths(routes: RouteObject[]): string[] {
    return routes.flatMap((route) => [
      ...(route.path && route.path !== '*' ? [route.path.replace(/:\w+/g, ID)] : []),
      ...mountedPaths(route.children ?? []),
    ])
  }

  /** Whether the real router has a page at `path`, rather than the not-found page. */
  function isMounted(path: string): boolean {
    const matches = matchRoutes(router.routes, path) ?? []
    const leaf = matches[matches.length - 1]
    return leaf !== undefined && leaf.route.path !== '*'
  }

  function renderTrail(path: string) {
    render(
      <MemoryRouter initialEntries={[path]}>
        <Breadcrumbs />
      </MemoryRouter>,
    )
    const trail = screen.getByRole('navigation', { name: 'Breadcrumb' })
    return {
      trail,
      links: within(trail)
        .queryAllByRole('link')
        .map((a) => [a.textContent, a.getAttribute('href')]),
    }
  }

  it.each(mountedPaths(router.routes))('%s: no crumb links to a path that is not a page', (path) => {
    const { trail, links } = renderTrail(path)

    for (const [, href] of links) expect(isMounted(href ?? '')).toBe(true)
    // A record id is never shown to the reader.
    expect(trail.textContent).not.toContain(ID)
  })

  it.each([
    ['/patients/:id', [['Patients', '/patients']], 'PatientsDetails'],
    ['/doctors/:id', [['Doctors', '/doctors']], 'DoctorsDetails'],
    ['/billing/:id', [['Billing', '/billing']], 'BillingDetails'],
    // /laboratory/orders is not a route: lab orders are listed at /laboratory.
    [
      '/laboratory/orders/:id',
      [
        ['Laboratory', '/laboratory'],
        ['Orders', '/laboratory'],
      ],
      'LaboratoryOrdersDetails',
    ],
    [
      '/pharmacy/prescriptions/:id',
      [
        ['Pharmacy', '/pharmacy'],
        ['Prescriptions', '/pharmacy/prescriptions'],
      ],
      'PharmacyPrescriptionsDetails',
    ],
    [
      '/pharmacy/medicines/:id',
      [
        ['Pharmacy', '/pharmacy'],
        ['Medicines', '/pharmacy/medicines'],
      ],
      'PharmacyMedicinesDetails',
    ],
    [
      '/pharmacy/purchase-orders/:id',
      [
        ['Pharmacy', '/pharmacy'],
        ['Purchase orders', '/pharmacy/purchase-orders'],
      ],
      'PharmacyPurchase ordersDetails',
    ],
    [
      '/inventory/purchase-orders/:id',
      [
        ['Inventory', '/inventory'],
        ['Purchase orders', '/inventory/purchase-orders'],
      ],
      'InventoryPurchase ordersDetails',
    ],
  ])('%s ends in "Details" and links back to its list', (pattern, expected, text) => {
    const { trail, links } = renderTrail(pattern.replace(':id', ID))

    expect(links).toEqual(expected)
    expect(trail).toHaveTextContent(text)
    // The last crumb is where the reader already is, so it is not a link.
    expect(within(trail).queryByRole('link', { name: 'Details' })).not.toBeInTheDocument()
  })

  it('does not link "Settings" from the profile page for someone who would be bounced off it', () => {
    // /settings is behind settings.read, which only the administrator holds
    // (backend/app/seeds/seed.py); /settings/profile is open to everyone.
    signIn(['notification.read.own', 'patient.read', 'appointment.book'])
    const { trail, links } = renderTrail('/settings/profile')

    expect(links).toEqual([])
    expect(trail).toHaveTextContent('SettingsProfile')
  })

  it('links "Settings" from the profile page for someone who can open it', () => {
    signIn(['settings.read'])
    const { links } = renderTrail('/settings/profile')

    expect(links).toEqual([['Settings', '/settings']])
  })

  it('shows a path with no page as plain text rather than a dead link', () => {
    const { links } = renderTrail(`/laboratory/unknown-area/${ID}`)

    expect(links).toEqual([['Laboratory', '/laboratory']])
    expect(screen.getByText('Unknown Area')).toBeInTheDocument()
  })
})

describe('row links', () => {
  const FIRST = '2026-10-05T09:00:00Z'
  const SECOND = '2026-10-05T14:30:00Z'

  function renderTable(table: React.ReactNode) {
    signIn([])
    render(<MemoryRouter>{table}</MemoryRouter>)
  }

  const labOrder = (id: string, ordered_at: string): LabOrder => ({
    id,
    appointment_id: null,
    patient_id: 'p1',
    patient_name: 'Ananya Rao',
    patient_mrn: 'MRN-2026-00007',
    doctor_id: 'd1',
    doctor_name: 'Dr. Priya Sharma',
    ordered_at,
    priority: 'routine',
    status: 'ordered',
    notes: null,
    collected_at: null,
    results_entered_at: null,
    released_at: null,
    released_by: null,
    cancelled_at: null,
    cancel_reason: null,
    invoice_id: null,
    turnaround_minutes: null,
    has_abnormal: false,
    has_critical: false,
    items: [],
  })

  const prescription = (id: string, prescribed_at: string): Prescription => ({
    id,
    appointment_id: 'a1',
    patient_id: 'p1',
    patient_name: 'Ananya Rao',
    patient_mrn: 'MRN-2026-00007',
    doctor_id: 'd1',
    doctor_name: 'Dr. Priya Sharma',
    status: 'active',
    notes: null,
    prescribed_at,
    cancelled_at: null,
    cancel_reason: null,
    items: [],
  })

  it('names each lab order of one patient by when it was ordered', () => {
    renderTable(<DataTable columns={labOrderColumns} data={[labOrder('o1', FIRST), labOrder('o2', SECOND)]} />)

    expect(
      screen.getByRole('link', { name: `Open lab order for Ananya Rao, ordered ${formatDateTime(FIRST)}` }),
    ).toHaveAttribute('href', '/laboratory/orders/o1')
    expect(
      screen.getByRole('link', { name: `Open lab order for Ananya Rao, ordered ${formatDateTime(SECOND)}` }),
    ).toHaveAttribute('href', '/laboratory/orders/o2')
  })

  it('names each prescription of one patient by when it was prescribed', () => {
    renderTable(
      <DataTable columns={prescriptionColumns} data={[prescription('rx1', FIRST), prescription('rx2', SECOND)]} />,
    )

    expect(
      screen.getByRole('link', {
        name: `Open prescription for Ananya Rao, prescribed ${formatDateTime(FIRST)}`,
      }),
    ).toHaveAttribute('href', '/pharmacy/prescriptions/rx1')
    expect(
      screen.getByRole('link', {
        name: `Open prescription for Ananya Rao, prescribed ${formatDateTime(SECOND)}`,
      }),
    ).toHaveAttribute('href', '/pharmacy/prescriptions/rx2')
  })
})

describe('dashboard greeting', () => {
  // The dashboard-relevant subset of each role's seeded permissions (backend/app/seeds/seed.py).
  const LAB_TECHNICIAN = ['notification.read.own', 'lab.test.read', 'lab.order.read', 'lab.order.enter_results']
  const PHARMACIST = ['notification.read.own', 'pharmacy.medicine.read', 'pharmacy.prescription.read']
  const INVENTORY_MANAGER = ['notification.read.own', 'inventory.item.read', 'inventory.stock.read']
  const RECEPTIONIST = [
    'patient.read',
    'patient.create',
    'appointment.read',
    'appointment.book',
    'invoice.read',
    'invoice.payment.record.cash',
    'doctor.read',
    'report.reception.read',
  ]
  const FRONT_DESK_WORDS = /register patients|book appointments|day's queue|awaiting payment/

  it.each([
    ['a lab technician', LAB_TECHNICIAN],
    ['a pharmacist', PHARMACIST],
    ['an inventory manager', INVENTORY_MANAGER],
  ])('does not tell %s to do front-desk work, and asks the server for nothing', async (_who, permissions) => {
    renderIn(<DashboardPage />, permissions)

    expect(
      await screen.findByText('Your home page — open a module from the menu to start work.'),
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(FRONT_DESK_WORDS)
    // No tile, no queue, no invoices: nothing this role may not read is requested.
    expect(fake.sent).toHaveLength(0)
    expect(screen.queryByText(/Couldn't load|couldn't be loaded/)).not.toBeInTheDocument()
  })

  it('tells a receptionist what their dashboard offers', async () => {
    renderIn(<DashboardPage />, RECEPTIONIST)

    expect(
      await screen.findByText(
        "Your operations hub — register patients, book appointments, follow the day's queue, and see invoices awaiting payment.",
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Register Patient/ })).toBeInTheDocument()
    expect(await screen.findByRole('region', { name: 'Awaiting payment' })).toBeInTheDocument()
    // Their own dashboard, and no other.
    expect(await screen.findByRole('region', { name: 'Front desk' })).toBeInTheDocument()
    expect(fake.requests('get', '/dashboards/').map((c) => c.url)).toEqual(['/dashboards/reception'])
  })

  it('names only the queue for a nurse, who can neither register nor book', async () => {
    renderIn(<DashboardPage />, ['patient.read', 'appointment.read', 'appointment.check_in', 'doctor.read'])

    expect(await screen.findByText("Your operations hub — follow the day's queue.")).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Register Patient/ })).not.toBeInTheDocument()
  })

  it('names only invoices for billing staff, and requests no appointments', async () => {
    renderIn(<DashboardPage />, [
      'patient.read',
      'doctor.read',
      'invoice.read',
      'invoice.payment.record',
      'report.billing.read',
      'report.export',
    ])

    expect(
      await screen.findByText('Your operations hub — see invoices awaiting payment.'),
    ).toBeInTheDocument()
    expect(await screen.findByRole('region', { name: 'Billing' })).toBeInTheDocument()
    expect(fake.requests('get', '/dashboards/').map((c) => c.url)).toEqual(['/dashboards/billing'])
    expect(fake.sent.some((c) => (c.url ?? '').includes('appointment'))).toBe(false)
  })

  it('shows only counts the API returned, to a user with no report code', async () => {
    fake.restore()
    fake = installFakeApi((config) =>
      config.url === '/patients' ? paged([], 137) : config.url === '/doctors' ? paged([], 5) : paged([], 0),
    )
    renderIn(<DashboardPage />, ['patient.read', 'doctor.read', 'appointment.read'])

    const tile = (label: string) => screen.getAllByText(label)[0].closest('div')?.parentElement as HTMLElement
    expect(await screen.findByText('137')).toBeInTheDocument()
    expect(within(tile('Total patients')).getByText('137')).toBeInTheDocument()
    expect(within(tile('Doctors')).getByText('5')).toBeInTheDocument()
    // Three reads, one per tile; the queue reuses the appointments read. No dashboard is asked for.
    expect(fake.sent.map((c) => c.url).sort()).toEqual(['/appointments', '/doctors', '/patients'])
  })
})

describe('page subtitles', () => {
  it('Settings names what it has: the hospital and the audit log', async () => {
    renderIn(<SettingsPage />, ['settings.read', 'audit.read'])

    expect(screen.getByText('Hospital configuration and the audit log.')).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Hospital/ })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Audit log/ })).toBeInTheDocument()
    expect(screen.getAllByRole('tab')).toHaveLength(2)
    expect(screen.queryByText(/departments/i)).not.toBeInTheDocument()
  })

  it('Doctors does not promise availability, which no screen shows', async () => {
    // Rendered with every doctor permission the administrator holds, so a
    // control gated on any of them would show.
    renderIn(<DoctorsPage />, [
      'doctor.read',
      'doctor.create',
      'doctor.update',
      'doctor.delete',
      'doctor.availability.read',
      'doctor.availability.update',
      'doctor.leave.create',
      'doctor.leave.delete',
      'department.read',
    ])

    expect(
      await screen.findByText('Clinical directory — specialties, departments and consultation fees.'),
    ).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/availability|leave/i)
    // Even so there is no control to add a doctor: managing only adds the inactive filter.
    expect(screen.getByRole('checkbox', { name: 'Include inactive' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /add|new|create/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /add|new|create/i })).not.toBeInTheDocument()
  })
})
