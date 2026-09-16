import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { SessionRestorer } from './SessionRestorer'
import { useAuthStore } from '@/store/auth-store'
import { tokenStore } from '@/services/tokenStore'

// Mock the api module
vi.mock('@/lib/api', () => ({
  api: {
    post: vi.fn(),
    get: vi.fn(),
  },
}))

describe('SessionRestorer', () => {
  beforeEach(() => {
    // Reset store
    useAuthStore.getState().logout()
    useAuthStore.getState().setRestoring(false)
    tokenStore.clear()
    vi.clearAllMocks()
  })

  it('renders children immediately when there is no refresh token', () => {
    // No refresh token set
    render(
      <SessionRestorer>
        <div>Child content</div>
      </SessionRestorer>,
    )

    // Children should render immediately — no spinner
    expect(screen.getByText('Child content')).toBeInTheDocument()
    expect(screen.queryByText('Restoring session…')).not.toBeInTheDocument()
  })

  it('shows spinner while restoring when refresh token is present', async () => {
    // Set a refresh token so the restore flow starts
    tokenStore.setRefreshToken('test-refresh-token')
    useAuthStore.getState().setRestoring(true)

    const { api } = await import('@/lib/api')
    // Make the refresh call hang so we can observe the spinner
    ;(api.post as ReturnType<typeof vi.fn>).mockReturnValue(new Promise(() => {}))

    render(
      <SessionRestorer>
        <div>Child content</div>
      </SessionRestorer>,
    )

    expect(screen.getByText('Restoring session…')).toBeInTheDocument()
    expect(screen.queryByText('Child content')).not.toBeInTheDocument()
  })

  it('renders children after failed restore', async () => {
    tokenStore.setRefreshToken('test-refresh-token')

    const { api } = await import('@/lib/api')
    ;(api.post as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('Network error'))

    render(
      <SessionRestorer>
        <div>Child content</div>
      </SessionRestorer>,
    )

    await waitFor(() => {
      expect(screen.getByText('Child content')).toBeInTheDocument()
    })

    expect(screen.queryByText('Restoring session…')).not.toBeInTheDocument()
  })

  it('skips restore when already authenticated', () => {
    useAuthStore.getState().setAuth(
      { id: '1', name: 'Test', email: 't@e.com', role: 'hospital_admin', permissions: [] },
      'token',
    )

    render(
      <SessionRestorer>
        <div>Child content</div>
      </SessionRestorer>,
    )

    expect(screen.getByText('Child content')).toBeInTheDocument()
    expect(screen.queryByText('Restoring session…')).not.toBeInTheDocument()
  })

  it('does not forge a user identity — fetches /users/me after refresh', async () => {
    tokenStore.setRefreshToken('test-refresh-token')

    const { api } = await import('@/lib/api')
    ;(api.post as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        data: {
          access_token: 'new-access',
          refresh_token: 'new-refresh',
        },
      },
    })
    ;(api.get as ReturnType<typeof vi.fn>).mockResolvedValue({
      data: {
        data: {
          id: '42',
          first_name: 'Dr. Smith',
          email: 'smith@hospital.com',
          roles: ['Doctor'],
          permissions: ['patient.read'],
        },
      },
    })

    render(
      <SessionRestorer>
        <div>Child content</div>
      </SessionRestorer>,
    )

    await waitFor(() => {
      expect(screen.getByText('Child content')).toBeInTheDocument()
    })

    // Verify /users/me was called
    expect(api.get).toHaveBeenCalledWith('/users/me')

    // Verify the real user was set, not a forged one
    const { user, isAuthenticated } = useAuthStore.getState()
    expect(isAuthenticated).toBe(true)
    expect(user?.id).toBe('42')
    expect(user?.role).toBe('doctor')
    expect(user?.name).toBe('Dr. Smith')
  })
})
