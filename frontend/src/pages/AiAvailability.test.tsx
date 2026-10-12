import { describe, it, expect, afterEach, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import DashboardLayout from '@/layouts/DashboardLayout'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, ok, paged, type FakeApi } from '@/test/fakeApi'
import LandingPage from './LandingPage'
import PricingPage from './PricingPage'
import PatientsPage from './patients/PatientsPage'

/**
 * One AI feature exists: an advisory, opt-in slot suggestion inside the
 * booking dialog. There is no assistant, so the shell and the public pages
 * must not offer one, and must not describe the one feature as more than it
 * is. These tests hold them to that.
 */

/** Wording that would describe an AI capability as something the product does today. */
const AI_CLAIMS = [
  /copilot/i,
  /ask aetheris/i,
  /how can i help/i,
  /context-aware/i,
  /summari[sz]e patient/i,
  /AI-assisted/i,
  /high-risk patients/i,
  /revenue summary/i,
  /the AI (reads|flags|reviews)/i,
  /agentic/i,
  /AI agent/i,
  /AI tools/i,
]

function expectNoAiClaims() {
  const text = document.body.textContent ?? ''
  for (const claim of AI_CLAIMS) expect(text).not.toMatch(claim)
}

let fake: FakeApi

beforeEach(() => {
  fake = installFakeApi((config) =>
    config.url === '/notifications/unread-count' ? ok({ unread: 0 }) : paged([]),
  )
})

afterEach(() => {
  fake.restore()
  signOut()
})

function renderShell(permissions: string[], page = <p>Page body</p>) {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={['/patients']}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route element={<DashboardLayout />}>
            <Route path="/patients" element={page} />
          </Route>
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

describe('the signed-in shell', () => {
  it('names the one AI feature as limited, and does not offer it as a control', async () => {
    renderShell(['patient.read'])
    await screen.findByText('Page body')

    const notice = screen.getByRole('note', { name: 'AI assistance is limited to slot suggestions when booking, where enabled' })
    expect(within(notice).getByText('AI assistance')).toBeInTheDocument()
    expect(within(notice).getByText('Slot suggestions only, where enabled')).toBeInTheDocument()
    // A notice, not something to click: nothing inside it is interactive.
    expect(within(notice).queryByRole('button')).not.toBeInTheDocument()
    expect(within(notice).queryByRole('link')).not.toBeInTheDocument()
    expect(notice.closest('button, a')).toBeNull()
  })

  it('has no assistant launcher, prompt, composer or send button anywhere', async () => {
    renderShell(['patient.read'])
    await screen.findByText('Page body')

    expect(screen.queryByRole('button', { name: /copilot|assistant|\bAI\b/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^send$/i })).not.toBeInTheDocument()
    expect(screen.queryByPlaceholderText(/ask/i)).not.toBeInTheDocument()
    for (const prompt of [
      "Show today's appointments",
      'Summarize patient history',
      'List high-risk patients',
      'Generate revenue summary',
      'Find pending invoices',
    ]) {
      expect(screen.queryByText(prompt)).not.toBeInTheDocument()
    }
    expectNoAiClaims()
  })

  it('opens no assistant when the notice is clicked, and calls no AI endpoint', async () => {
    const user = userEvent.setup()
    renderShell(['patient.read'])
    await screen.findByText('Page body')

    await user.click(screen.getByRole('note', { name: 'AI assistance is limited to slot suggestions when booking, where enabled' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(fake.sent.some((c) => /\/ai\b|recommend/.test(c.url ?? ''))).toBe(false)
  })

  it('promises nothing as "coming soon": no placeholder control anywhere in the shell', async () => {
    renderShell(['patient.read', 'appointment.read', 'notification.read.own'])
    await screen.findByText('Page body')

    // Visible text and every attribute — placeholder, title, aria-label.
    expect(document.body.innerHTML).not.toMatch(/coming soon/i)
    // The top bar's dead search box is gone; nothing in the bar is disabled.
    const bar = screen.getByRole('banner')
    expect(within(bar).queryByRole('textbox')).not.toBeInTheDocument()
    expect(bar.querySelector(':disabled')).toBeNull()
    // AI is still named, and only for what it does.
    expect(screen.getByRole('note', { name: 'AI assistance is limited to slot suggestions when booking, where enabled' })).toBeInTheDocument()
  })

  it('keeps the rest of the shell working: navigation still follows permissions', async () => {
    renderShell(['patient.read', 'appointment.read', 'notification.read.own'])
    await screen.findByText('Page body')

    // The desktop rail (the breadcrumbs are a navigation landmark too).
    const nav = screen.getByRole('complementary')
    expect(within(nav).getByRole('link', { name: /Patients/ })).toHaveAttribute('href', '/patients')
    expect(within(nav).getByRole('link', { name: /Appointments/ })).toBeInTheDocument()
    expect(within(nav).queryByRole('link', { name: /Billing/ })).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /^Notifications/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeInTheDocument()
  })
})

describe('the Patients page', () => {
  it('describes the registry without claiming AI summaries or admissions', async () => {
    renderShell(['patient.read'], <PatientsPage />)

    expect(await screen.findByRole('heading', { name: 'Patients' })).toBeInTheDocument()
    expect(screen.getByText(/The patient registry/)).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/admissions/i)
    expectNoAiClaims()
  })
})

describe('the public pages', () => {
  function renderPublic(page: React.ReactNode) {
    render(<MemoryRouter>{page}</MemoryRouter>)
  }

  it('landing: describes the one AI feature as optional', () => {
    renderPublic(<LandingPage />)

    expectNoAiClaims()
    const roadmap = screen.getByRole('heading', { name: 'On the roadmap' }).parentElement as HTMLElement
    expect(roadmap).toHaveTextContent(
      'More AI assistance is planned. Today the only AI feature is an optional slot suggestion when booking, where it has been enabled.',
    )
    expect(screen.getByText('Does Aetheris include AI features today?')).toBeInTheDocument()
    expect(screen.queryByText(/intelligent platform/i)).not.toBeInTheDocument()
  })

  it('landing: the AI answer names one optional feature, and the list of modules names reports but no AI', async () => {
    const user = userEvent.setup()
    renderPublic(<LandingPage />)

    await user.click(screen.getByRole('button', { name: 'Does Aetheris include AI features today?' }))
    expect(
      await screen.findByText(
        /^One, and it is optional\. When booking an appointment, staff can ask for an AI-suggested time slot/,
      ),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Which modules are available today?' }))
    const modules = await screen.findByText(/^Patients, doctors and departments, appointments, billing/)
    expect(modules).toHaveTextContent(/reports and role dashboards/)
    // Reports are built, so the answer no longer calls them planned — and it offers no AI.
    expect(modules.textContent).not.toMatch(/planned|not part of the product/i)
    expect(modules.textContent).not.toMatch(/\bAI\b|artificial intelligence|assistant|copilot/i)
  })

  it('pricing: sells no AI features', () => {
    renderPublic(<PricingPage />)

    expectNoAiClaims()
    // AI appears once, under what is planned — never under what is included.
    const planned = screen.getByRole('region', { name: 'On the roadmap' })
    expect(within(planned).getByText('AI assistance beyond slot suggestions')).toBeInTheDocument()
    const included = screen.getByRole('region', { name: 'Included today' })
    expect(included.textContent).not.toMatch(/\bAI\b/)
    expect(included.textContent).not.toMatch(/analytics/i)
    // Planned is exactly more AI assistance: Reports has left the roadmap.
    expect(within(planned).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'AI assistance beyond slot suggestions',
    ])
    expect(within(planned).getByText('Planned, and not available yet.')).toBeInTheDocument()
  })
})
