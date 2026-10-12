import { Suspense, type ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import Breadcrumbs from '@/components/layout/Breadcrumbs'
import { MOCK_PERMISSIONS_BY_ROLE, navForPermissions, type Permission, type Role } from '@/lib/rbac'
import { router } from '@/router'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, ok, paged, type FakeApi } from '@/test/fakeApi'
import {
  appointmentsReportFixture,
  outstandingReportFixture,
  patientsReportFixture,
  revenueReportFixture,
} from '@/test/reportFixtures'
import { ReportsTabs } from './ReportsTabs'

/**
 * How Reports is reached from the rest of the app: the sidebar entry, the
 * guard on each of the five routes — taken from the real router, not a copy of
 * it — the section tabs, and who is offered the export. Role permission sets
 * mirror `backend/app/seeds/seed.py`.
 */

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const perms = (role: Role): Permission[] => MOCK_PERMISSIONS_BY_ROLE[role]

const REPORT_PATHS = [
  '/reports',
  '/reports/patients',
  '/reports/appointments',
  '/reports/revenue',
  '/reports/outstanding',
] as const
type ReportPath = (typeof REPORT_PATHS)[number]

const ANSWERS: Record<string, unknown> = {
  '/reports/patients': patientsReportFixture,
  '/reports/appointments': appointmentsReportFixture,
  '/reports/revenue': revenueReportFixture,
  '/reports/outstanding': outstandingReportFixture,
}

let fake: FakeApi

beforeEach(() => {
  fake = installFakeApi((config) => {
    const data = ANSWERS[config.url ?? '']
    return data ? ok(data) : paged([])
  })
})

afterEach(() => {
  fake.restore()
  signOut()
})

interface RouteNode {
  path?: string
  element?: ReactNode
  children?: RouteNode[]
}

/** The element the real router mounts at `path`, guard included. */
function routeElement(path: string, nodes: RouteNode[] = router.routes as RouteNode[]): ReactNode {
  for (const node of nodes) {
    if (node.path === path) return node.element
    const nested = node.children ? routeElement(path, node.children) : undefined
    if (nested) return nested
  }
  return undefined
}

function Where() {
  return <p>at {useLocation().pathname}</p>
}

function open(path: ReportPath, permissions: string[]) {
  signIn(permissions)
  const element = routeElement(path)
  if (!element) throw new Error(`The router has no route at ${path}`)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[path]}>
        <Suspense fallback={<p>loading the page</p>}>
          <Routes>
            <Route path={path} element={element} />
            <Route path="*" element={<Where />} />
          </Routes>
        </Suspense>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const reportRequests = () =>
  fake.sent.filter((r) => /^\/(reports|dashboards)\//.test(r.url ?? '')).map((r) => r.url)

const HEADINGS: Record<ReportPath, string> = {
  '/reports': 'Reports',
  '/reports/patients': 'Patients report',
  '/reports/appointments': 'Appointments report',
  '/reports/revenue': 'Revenue report',
  '/reports/outstanding': 'Outstanding report',
}

describe('reports in the sidebar', () => {
  it.each<[Role, boolean]>([
    ['super_admin', true],
    ['hospital_admin', true],
    ['billing_staff', true],
    // A dashboard code is not a report code.
    ['doctor', false],
    ['receptionist', false],
    ['nurse', false],
    ['lab_technician', false],
    ['pharmacist', false],
    ['inventory_manager', false],
  ])('%s sees the Reports entry: %s', (role, expected) => {
    const paths = navForPermissions(perms(role)).map((n) => n.to)
    expect(paths.includes('/reports')).toBe(expected)
  })

  it('no seeded role still holds the retired report.read code', () => {
    for (const codes of Object.values(MOCK_PERMISSIONS_BY_ROLE)) {
      expect((codes as string[]).includes('report.read')).toBe(false)
    }
  })

  it('each role holds exactly the report codes it is seeded with', () => {
    const reportCodes = (role: Role) => perms(role).filter((code) => code.startsWith('report.')).sort()
    const all = [
      'report.admin.read',
      'report.billing.read',
      'report.doctor.read',
      'report.export',
      'report.reception.read',
    ]
    expect(reportCodes('super_admin')).toEqual(all)
    expect(reportCodes('hospital_admin')).toEqual(all)
    expect(reportCodes('doctor')).toEqual(['report.doctor.read'])
    expect(reportCodes('receptionist')).toEqual(['report.reception.read'])
    expect(reportCodes('billing_staff')).toEqual(['report.billing.read', 'report.export'])
    for (const role of ['nurse', 'lab_technician', 'pharmacist', 'inventory_manager'] as const) {
      expect(reportCodes(role)).toEqual([])
    }
  })
})

describe('the report routes', () => {
  it.each(REPORT_PATHS)('%s opens for a hospital admin', async (path) => {
    open(path, perms('hospital_admin'))

    expect(await screen.findByRole('heading', { level: 1, name: HEADINGS[path] })).toBeInTheDocument()
    // Each report asks for its own figures and nothing else's; the landing page asks for none.
    const expected = path === '/reports' ? [] : [path]
    if (expected.length) await screen.findByText(/All figures in Asia\/Kolkata/)
    expect(reportRequests()).toEqual(expected)
  })

  it.each<ReportPath>(['/reports', '/reports/revenue', '/reports/outstanding'])(
    '%s opens for billing staff',
    async (path) => {
      open(path, perms('billing_staff'))

      expect(await screen.findByRole('heading', { level: 1, name: HEADINGS[path] })).toBeInTheDocument()
      if (path !== '/reports') await screen.findByText(/All figures in Asia\/Kolkata/)
      expect(reportRequests()).toEqual(path === '/reports' ? [] : [path])
    },
  )

  it.each<ReportPath>(['/reports/patients', '/reports/appointments'])(
    '%s sends billing staff back to the dashboard, asking the API nothing',
    async (path) => {
      open(path, perms('billing_staff'))

      expect(await screen.findByText('at /dashboard')).toBeInTheDocument()
      expect(fake.sent).toHaveLength(0)
    },
  )

  describe.each<Role>(['doctor', 'receptionist', 'nurse', 'pharmacist'])('a %s', (role) => {
    it.each(REPORT_PATHS)('is sent back to the dashboard from %s, asking the API nothing', async (path) => {
      open(path, perms(role))

      expect(await screen.findByText('at /dashboard')).toBeInTheDocument()
      expect(fake.sent).toHaveLength(0)
    })
  })

  it('does not open for the export code alone', async () => {
    open('/reports/revenue', ['report.export'])

    expect(await screen.findByText('at /dashboard')).toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)
  })
})

describe('the Reports landing page', () => {
  const cards = () =>
    within(screen.getByRole('list', { name: 'Reports' }))
      .getAllByRole('link')
      .map((link) => link.getAttribute('href'))

  it('offers an administrator all four reports', async () => {
    open('/reports', perms('hospital_admin'))

    await screen.findByRole('heading', { level: 1, name: 'Reports' })
    expect(
      screen.getByText('Registrations, appointments, revenue and unpaid invoices for your hospital.'),
    ).toBeInTheDocument()
    expect(cards()).toEqual(['/reports/patients', '/reports/appointments', '/reports/revenue', '/reports/outstanding'])
  })

  it('offers billing staff the two financial reports', async () => {
    open('/reports', perms('billing_staff'))

    await screen.findByRole('heading', { level: 1, name: 'Reports' })
    expect(cards()).toEqual(['/reports/revenue', '/reports/outstanding'])
    expect(screen.queryByText(/Patients registered per day/)).not.toBeInTheDocument()
  })
})

describe('report section tabs', () => {
  function tabsFor(permissions: string[]) {
    signIn(permissions)
    render(
      <MemoryRouter initialEntries={['/reports/revenue']}>
        <ReportsTabs />
      </MemoryRouter>,
    )
    const nav = screen.queryByRole('navigation', { name: 'Report sections' })
    return nav ? within(nav).getAllByRole('link') : []
  }

  it('lists every report for an administrator and marks the open one', () => {
    const tabs = tabsFor(perms('hospital_admin'))

    expect(tabs.map((a) => a.textContent)).toEqual(['Overview', 'Patients', 'Appointments', 'Revenue', 'Outstanding'])
    expect(tabs.map((a) => a.getAttribute('href'))).toEqual([...REPORT_PATHS])
    // Overview's path starts every other; only the open report is current.
    expect(tabs.filter((a) => a.getAttribute('aria-current') === 'page').map((a) => a.textContent)).toEqual([
      'Revenue',
    ])
  })

  it('lists only Overview, Revenue and Outstanding for billing staff', () => {
    expect(tabsFor(perms('billing_staff')).map((a) => a.textContent)).toEqual(['Overview', 'Revenue', 'Outstanding'])
  })

  it('lists nothing for a role with a dashboard code only', () => {
    expect(tabsFor(perms('doctor'))).toEqual([])
  })
})

describe('the export button', () => {
  it('is offered to a hospital admin and to billing staff', async () => {
    open('/reports/revenue', perms('billing_staff'))

    await screen.findByText(/All figures in Asia\/Kolkata/)
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeInTheDocument()
  })

  it.each<ReportPath>(['/reports/patients', '/reports/appointments', '/reports/revenue', '/reports/outstanding'])(
    'is absent from %s without report.export',
    async (path) => {
      open(path, ['report.admin.read', 'report.billing.read'])

      await screen.findByText(/All figures in Asia\/Kolkata/)
      expect(screen.queryByRole('button', { name: /export/i })).not.toBeInTheDocument()
      expect(fake.sent.some((r) => (r.url ?? '').includes('/export'))).toBe(false)
    },
  )
})

describe('breadcrumbs on a report', () => {
  it.each([
    ['/reports/patients', 'Patients'],
    ['/reports/appointments', 'Appointments'],
    ['/reports/revenue', 'Revenue'],
    ['/reports/outstanding', 'Outstanding'],
  ])('%s is Reports, linked, then %s', (path, label) => {
    signIn(perms('hospital_admin'))
    render(
      <MemoryRouter initialEntries={[path]}>
        <Breadcrumbs />
      </MemoryRouter>,
    )

    const trail = screen.getByRole('navigation', { name: 'Breadcrumb' })
    expect(within(trail).getByRole('link', { name: 'Reports' })).toHaveAttribute('href', '/reports')
    expect(within(trail).getByText(label)).toBeInTheDocument()
    expect(within(trail).queryByRole('link', { name: label })).not.toBeInTheDocument()
  })
})
