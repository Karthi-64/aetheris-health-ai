import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { RequirePermission } from '@/components/auth/RequirePermission'
import { isInAppPath, kindPresentation } from '@/components/notifications/kinds'
import { MOCK_PERMISSIONS_BY_ROLE, navForPermissions, type Permission, type Role } from '@/lib/rbac'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, paged, type FakeApi } from '@/test/fakeApi'
import InventoryIndexPage from './InventoryIndexPage'
import { InventoryTabs } from './InventoryTabs'

/**
 * How Inventory is reached from the rest of the app: the sidebar entry, what
 * `/inventory` — the low-stock notification's link — shows each role, and the
 * section tabs. Role permission sets mirror `backend/app/seeds/seed.py`.
 */

vi.mock('sonner', () => ({ toast: { success: vi.fn(), error: vi.fn() } }))

const perms = (role: Role): Permission[] => MOCK_PERMISSIONS_BY_ROLE[role]

let fake: FakeApi

beforeEach(() => {
  fake = installFakeApi(() => paged([]))
})

afterEach(() => {
  fake.restore()
  signOut()
})

function Where() {
  return <p>at {useLocation().pathname}</p>
}

function renderIndex(permissions: string[]) {
  signIn(permissions)
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={['/inventory']}>
        <Routes>
          <Route
            path="/inventory"
            element={
              <RequirePermission group="inventory.stock.read">
                <InventoryIndexPage />
              </RequirePermission>
            }
          />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

const inventoryRequests = () => fake.sent.filter((r) => (r.url ?? '').includes('/inventory'))

describe('inventory in the sidebar', () => {
  it.each<[Role, boolean]>([
    ['hospital_admin', true],
    ['super_admin', true],
    ['inventory_manager', true],
    // Read-only for a pharmacist; a nurse also records what the ward uses.
    ['pharmacist', true],
    ['nurse', true],
    ['doctor', false],
    ['receptionist', false],
    ['billing_staff', false],
  ])('%s sees the Inventory entry: %s', (role, expected) => {
    const paths = navForPermissions(perms(role)).map((n) => n.to)
    expect(paths.includes('/inventory')).toBe(expected)
  })

  it('no seeded role still holds a retired inventory code', () => {
    const retired = ['inventory.read', 'inventory.create', 'inventory.update']
    for (const codes of Object.values(MOCK_PERMISSIONS_BY_ROLE)) {
      expect(codes.filter((c) => retired.includes(c))).toEqual([])
    }
  })
})

describe('/inventory', () => {
  it.each<Role>(['hospital_admin', 'inventory_manager', 'pharmacist', 'nurse'])(
    'shows a %s the stock overview, read from the summary endpoint',
    async (role) => {
      renderIndex(perms(role))

      expect(await screen.findByRole('heading', { level: 1, name: 'Inventory' })).toBeInTheDocument()
      expect(fake.requests('get', '/inventory/stock/summary').length).toBeGreaterThan(0)
    },
  )

  it.each<Role>(['doctor', 'receptionist', 'billing_staff'])(
    'sends a %s back to the dashboard, asking the inventory API nothing',
    async (role) => {
      renderIndex(perms(role))

      expect(await screen.findByText('at /dashboard')).toBeInTheDocument()
      expect(inventoryRequests()).toHaveLength(0)
    },
  )

  it('sends a role with only the item list to that list, without reading stock', async () => {
    renderIndex(['inventory.item.read'])

    expect(await screen.findByText('at /inventory/items')).toBeInTheDocument()
    expect(inventoryRequests()).toHaveLength(0)
  })
})

describe('inventory section tabs', () => {
  function tabsFor(role: Role) {
    signIn(perms(role))
    render(
      <MemoryRouter initialEntries={['/inventory']}>
        <InventoryTabs />
      </MemoryRouter>,
    )
    const nav = screen.queryByRole('navigation', { name: 'Inventory sections' })
    return nav ? within(nav).getAllByRole('link').map((a) => a.textContent) : []
  }

  it('lists every section for an inventory manager', () => {
    expect(tabsFor('inventory_manager')).toEqual([
      'Overview',
      'Stock',
      'Movements',
      'Items',
      'Locations',
      'Purchase orders',
    ])
  })

  it.each<Role>(['nurse', 'pharmacist'])('leaves purchase orders out for a %s', (role) => {
    expect(tabsFor(role)).toEqual(['Overview', 'Stock', 'Movements', 'Items', 'Locations'])
  })
})

describe('the low-stock notification', () => {
  it('is filed under Inventory and its link is one the app follows', () => {
    expect(kindPresentation('inventory.low_stock').category).toBe('Inventory')
    // The link the backend catalog sends (notification_catalog.py).
    expect(isInAppPath('/inventory')).toBe(true)
  })
})
