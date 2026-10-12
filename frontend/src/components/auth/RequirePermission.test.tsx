import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { RequirePermission } from './RequirePermission'
import { useAuthStore } from '@/store/auth-store'
import { MOCK_PERMISSIONS_BY_ROLE } from '@/lib/rbac'

function login(role: keyof typeof MOCK_PERMISSIONS_BY_ROLE) {
  useAuthStore.getState().setAuth(
    {
      id: '1',
      name: 'Test User',
      email: 'test@aetheris.health',
      role,
      permissions: [...MOCK_PERMISSIONS_BY_ROLE[role]],
    },
    'access-token',
  )
}

function renderBilling() {
  return render(
    <MemoryRouter initialEntries={['/billing']}>
      <Routes>
        <Route path="/dashboard" element={<div>Dashboard</div>} />
        <Route
          path="/billing"
          element={
            <RequirePermission permission="invoice.read">
              <div>Billing page</div>
            </RequirePermission>
          }
        />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RequirePermission', () => {
  beforeEach(() => useAuthStore.getState().logout())

  it('renders the route when the user has the permission', () => {
    login('billing_staff')
    renderBilling()
    expect(screen.getByText('Billing page')).toBeInTheDocument()
  })

  it('redirects to /dashboard when the user lacks the permission', () => {
    login('lab_technician') // no invoice.read
    renderBilling()
    expect(screen.queryByText('Billing page')).not.toBeInTheDocument()
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
  })

  it('admits either code of a permission group', () => {
    // A doctor holds invoice.read.own, not invoice.read. The nav shows Billing
    // for either, so the route has to as well.
    login('doctor')
    render(
      <MemoryRouter initialEntries={['/billing']}>
        <Routes>
          <Route path="/dashboard" element={<div>Dashboard</div>} />
          <Route
            path="/billing"
            element={
              <RequirePermission group="invoice.read">
                <div>Billing page</div>
              </RequirePermission>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('Billing page')).toBeInTheDocument()
  })

  it('redirects when the user holds no code in the group', () => {
    login('lab_technician')
    render(
      <MemoryRouter initialEntries={['/billing']}>
        <Routes>
          <Route path="/dashboard" element={<div>Dashboard</div>} />
          <Route
            path="/billing"
            element={
              <RequirePermission group="invoice.read">
                <div>Billing page</div>
              </RequirePermission>
            }
          />
        </Routes>
      </MemoryRouter>,
    )
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
  })
})
