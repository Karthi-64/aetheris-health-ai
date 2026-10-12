import { lazy, Suspense } from 'react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom'
import { router as appRouter } from '@/router'
import RouteErrorPage from './RouteErrorPage'

const SECRET = 'Cannot read properties of undefined (reading invoice_number)'

function Broken(): never {
  throw new Error(SECRET)
}

// What a browser reports when a page's chunk is gone after a redeploy.
const StaleChunk = lazy(() =>
  Promise.reject(new TypeError('Failed to fetch dynamically imported module: /assets/BillingPage-abc123.js')),
)

/** The same shape as the app's router: one root route owning the boundary. */
function renderAt(path: string) {
  const router = createMemoryRouter(
    [
      {
        element: (
          <Suspense fallback={<p>Loading</p>}>
            <Outlet />
          </Suspense>
        ),
        errorElement: <RouteErrorPage />,
        children: [
          { path: '/billing', element: <Broken /> },
          { path: '/stale', element: <StaleChunk /> },
          { path: '/dashboard', element: <h1>Dashboard</h1> },
        ],
      },
    ],
    { initialEntries: [path] },
  )
  return render(<RouterProvider router={router} />)
}

describe('RouteErrorPage', () => {
  beforeEach(() => {
    // React and the router both report the caught error; that is expected here.
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  afterEach(() => vi.restoreAllMocks())

  it('the app router has an error boundary at its root', () => {
    expect(appRouter.routes[0].hasErrorBoundary).toBe(true)
  })

  it('apologises for a page that throws, without the error text or a stack', async () => {
    const { container } = renderAt('/billing')

    expect(await screen.findByRole('heading', { name: 'Something went wrong' })).toBeInTheDocument()
    expect(container.textContent).not.toContain(SECRET)
    expect(container.textContent).not.toMatch(/Unexpected Application Error|at Broken|\.tsx/)
    expect(container.querySelector('pre')).toBeNull()
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to dashboard' })).toHaveAttribute('href', '/dashboard')
  })

  it('leads back to the dashboard without a reload', async () => {
    const user = userEvent.setup()
    renderAt('/billing')

    await user.click(await screen.findByRole('link', { name: 'Back to dashboard' }))

    expect(await screen.findByRole('heading', { name: 'Dashboard' })).toBeInTheDocument()
  })

  it('says the app may have been updated when a page’s code fails to load', async () => {
    const { container } = renderAt('/stale')

    expect(await screen.findByRole('heading', { name: 'This page could not be loaded' })).toBeInTheDocument()
    expect(screen.getByText(/may have been updated/)).toBeInTheDocument()
    expect(container.textContent).not.toMatch(/dynamically imported|BillingPage-abc123/)
    expect(screen.getByRole('button', { name: 'Reload' })).toBeInTheDocument()
  })
})
