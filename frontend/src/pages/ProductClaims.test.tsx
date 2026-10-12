import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { legalDocs } from '@/content/legal'
import { mailtoUrl } from '@/lib/mailto'
import { signIn, signOut } from '@/test/auth'
import { installFakeApi, paged, type FakeApi } from '@/test/fakeApi'
import ContactPage from './ContactPage'
import LandingPage from './LandingPage'
import LegalPage from './LegalPage'
import LoginPage from './LoginPage'
import PricingPage from './PricingPage'
import PatientsPage from './patients/PatientsPage'
import ReportsPage from './reports/ReportsPage'

/**
 * What the product says about itself must be something the repository backs.
 * These tests hold the public pages to that, and check that the controls that
 * did nothing are gone rather than left looking as if they work.
 */

const { toastSuccess, openEmailApp } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  openEmailApp: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: vi.fn() } }))
vi.mock('@/lib/mailto', async (original) => ({
  ...(await original<typeof import('@/lib/mailto')>()),
  openEmailApp,
}))

/** Capabilities and assurances the repository does not have. */
const UNSUPPORTED = [
  /SOC 2 Type II/i,
  /HIPAA (aligned|compliant)/i,
  /end-to-end/i,
  /on-premise|private cloud|own infrastructure|your own environment/i,
  /encrypted in transit and at rest/i,
  /Business Associate under/i,
  /vitals/i,
  /admission|admit a patient/i,
  /real-time|live dashboards|occupancy/i,
  /Pharmacy & Lab/i,
  // Pharmacy is real now; these are the parts of it that are not.
  /e-?prescri|electronic prescri/i,
  /interaction/i,
  /substitut/i,
  // Inventory is real now; forecasting and automatic ordering are not.
  /forecast|predict|auto(matic)?[- ]?(re)?order|barcode/i,
  /clinical decision-support/i,
  /integration seams/i,
  /a few weeks/i,
  /most popular/i,
  /\$\s?\d/,
  /uptime|99\.\d|guarantee/i,
  /trusted by|customers|hospitals use/i,
  /dedicated success engineer|priority support|community support/i,
  /production[- ]ready|enterprise[- ]ready|clinical-grade/i,
]

/** Named standards and audits may only appear in a sentence saying they are not held. */
const ONLY_IN_THE_NEGATIVE = /HIPAA|SOC 2|audited|certified|Business Associate/i

function expectHonest(text: string) {
  for (const claim of UNSUPPORTED) expect(text).not.toMatch(claim)
  for (const sentence of text.split(/(?<=[.?!])\s+/)) {
    if (ONLY_IN_THE_NEGATIVE.test(sentence)) expect(sentence).toMatch(/\bnot\b/)
  }
}

const pageText = () => document.body.textContent ?? ''

/**
 * Doctor availability and leave exist only as the open slots offered when
 * booking: no screen shows or edits a schedule, records leave, adds a doctor
 * or manages departments, so no public page may say one does.
 */
const NO_SCREEN_FOR = [
  /weekly availability/i,
  /\bleave\b/i,
  /departments?,? and availability/i,
  /(manage|managing|management of|edit|set|create|add) (doctor |doctors' |a doctor's )?(departments?|availability|schedules?|rosters?)/i,
  /department management|roster|shift/i,
  // Nobody publishes a schedule: booking offers the slots that are open.
  /publish(ed|es|ing)? (slots?|schedules?|availability|calendars?)/i,
]

function expectNoScheduleClaims(text: string) {
  for (const claim of NO_SCREEN_FOR) expect(text).not.toMatch(claim)
}

const BUILT = /laborator|pharmac|inventor/i
const NOT_BUILT_WORDING = /planned|roadmap|coming|not (yet )?available|not (a )?part of|soon|upcoming|future/i

/** No sentence may describe Laboratory, Pharmacy or Inventory as something still to come. */
function expectBuiltModulesNotPlanned(passages: string[]) {
  for (const passage of passages) {
    for (const sentence of passage.split(/(?<=[.?!])\s+/)) {
      if (BUILT.test(sentence)) expect(sentence).not.toMatch(NOT_BUILT_WORDING)
    }
  }
}

/** The text of each element that directly holds copy, one passage per element. */
const passagesOnPage = () =>
  Array.from(document.body.querySelectorAll('h1, h2, h3, p, li, span, [role="region"]'))
    .filter((el) => el.children.length === 0 || el.getAttribute('role') === 'region')
    .map((el) => el.textContent ?? '')

function renderPublic(page: React.ReactNode) {
  render(<MemoryRouter>{page}</MemoryRouter>)
}

let fake: FakeApi

beforeEach(() => {
  toastSuccess.mockReset()
  openEmailApp.mockReset()
  fake = installFakeApi(() => paged([]))
})

afterEach(() => {
  fake.restore()
  signOut()
})

describe('landing page', () => {
  it('makes no unsupported claim, in the page or in any FAQ answer', async () => {
    const user = userEvent.setup()
    renderPublic(<LandingPage />)

    expectHonest(pageText())
    // Answers are only in the document while their question is open.
    const questions = [
      'How is patient data protected?',
      'Which modules are available today?',
      'Does Aetheris include AI features today?',
      'Will it connect to our existing EHR and lab systems?',
    ]
    for (const question of questions) {
      await user.click(screen.getByRole('button', { name: question }))
      expectHonest(pageText())
    }
  })

  it('names the safeguards the application implements, not certifications', () => {
    renderPublic(<LandingPage />)

    for (const label of [
      'Role-based access',
      'Per-hospital data separation',
      'Hashed passwords, optional MFA',
      'Append-only audit log',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument()
    }
  })

  it('says plainly that it is not audited or certified', async () => {
    const user = userEvent.setup()
    renderPublic(<LandingPage />)

    await user.click(screen.getByRole('button', { name: 'How is patient data protected?' }))

    expect(
      await screen.findByText(/does not currently claim HIPAA compliance or a SOC 2 report/),
    ).toBeInTheDocument()
  })

  it('presents unbuilt modules as roadmap, and built ones as modules', () => {
    renderPublic(<LandingPage />)

    const roadmap = screen.getByRole('heading', { name: 'On the roadmap' }).parentElement as HTMLElement
    expect(roadmap.textContent).toBe(
      'On the roadmapMore AI assistance is planned. Today the only AI feature is an optional slot suggestion when booking, where it has been enabled.',
    )
    for (const built of [
      'Patients',
      'Appointments',
      'Billing',
      'Doctors & departments',
      'Laboratory',
      'Pharmacy',
      'Inventory',
      'Administration',
    ]) {
      expect(screen.getByRole('heading', { name: built })).toBeInTheDocument()
    }
    for (const unbuilt of [/Reports/]) {
      expect(screen.queryByRole('heading', { name: unbuilt })).not.toBeInTheDocument()
    }
  })

  it('describes doctors as a directory, and availability only as open slots when booking', () => {
    renderPublic(<LandingPage />)

    expectNoScheduleClaims(pageText())
    // The hero's Schedule step says what booking does, in the same words as the rest of the page.
    expect(screen.getByText("Book into a doctor's open slots and run the day's queue.")).toBeInTheDocument()
    expect(pageText()).not.toMatch(/published/i)
    const card = screen.getByRole('heading', { name: 'Doctors & departments' }).parentElement as HTMLElement
    expect(card).toHaveTextContent(
      "Doctor profiles with specialty and consultation fee, listed by department. A doctor's open slots are offered when booking.",
    )
    // The true statement stays: booking offers the doctor's open slots.
    expect(screen.getByText("Book the patient into one of the doctor's open slots.")).toBeInTheDocument()
  })

  it('introduces every team the product serves, not the first three modules', () => {
    renderPublic(<LandingPage />)

    expect(screen.queryByText(/Patients, scheduling, and billing in a single system/)).not.toBeInTheDocument()
    expect(
      screen.getByText(
        /Patients, appointments, billing, laboratory, pharmacy, and inventory in a single\s+system that your front desk, doctors, lab, pharmacy, stores, and billing staff all\s+share\./,
      ),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('heading', {
        name: /The front desk, the consulting room, the lab, the pharmacy, stores, and billing in\s+one place\./,
      }),
    ).toBeInTheDocument()
  })

  it('never describes Laboratory, Pharmacy or Inventory as planned', async () => {
    const user = userEvent.setup()
    renderPublic(<LandingPage />)

    expectBuiltModulesNotPlanned(passagesOnPage())
    for (const question of ['Which modules are available today?', 'Does Aetheris include AI features today?']) {
      await user.click(screen.getByRole('button', { name: question }))
      expectBuiltModulesNotPlanned(passagesOnPage())
    }
    const roadmap = screen.getByRole('heading', { name: 'On the roadmap' }).parentElement as HTMLElement
    expect(roadmap.textContent).not.toMatch(BUILT)
  })

  it('walks through the visit the product actually carries', () => {
    renderPublic(<LandingPage />)

    for (const step of ['Check in', 'Consult']) {
      expect(screen.getByRole('heading', { name: step })).toBeInTheDocument()
    }
    expect(screen.queryByRole('heading', { name: 'Diagnose' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Treat' })).not.toBeInTheDocument()
  })
})

describe('pricing page', () => {
  it('lists no plans, prices or invented features', () => {
    renderPublic(<PricingPage />)

    expectHonest(pageText())
    expect(screen.getByRole('heading', { name: 'Pricing' })).toBeInTheDocument()
    expect(screen.getByText(/does not have published plans yet/)).toBeInTheDocument()
    for (const plan of ['Starter', 'Professional', 'Enterprise']) {
      expect(screen.queryByText(plan)).not.toBeInTheDocument()
    }
    expect(screen.queryByRole('link', { name: /get started|upgrade/i })).not.toBeInTheDocument()
  })

  it('separates what is included today from what is planned', () => {
    renderPublic(<PricingPage />)

    const included = screen.getByRole('region', { name: 'Included today' })
    expect(within(included).getByText('Invoices, payments, and refunds')).toBeInTheDocument()
    expect(within(included).getByText('Audit log with export')).toBeInTheDocument()
    // Laboratory has real screens now, so it is listed as included, not planned.
    expect(within(included).getByText('Laboratory orders and results')).toBeInTheDocument()
    // So does Pharmacy — named for what it does, not as a bare module name.
    expect(
      within(included).getByText('Pharmacy prescribing, dispensing, and medicine stock'),
    ).toBeInTheDocument()
    const planned = screen.getByRole('region', { name: 'On the roadmap' })
    expect(within(planned).getByText('Planned, and not available yet.')).toBeInTheDocument()
    expect(
      within(included).getByText('Inventory of supplies by location, with purchase orders'),
    ).toBeInTheDocument()
    expect(within(planned).queryByText(/Pharmacy|Inventory|Laborator/)).not.toBeInTheDocument()
    expect(planned.textContent).not.toMatch(BUILT)
    expectBuiltModulesNotPlanned(passagesOnPage().filter((text) => text !== planned.textContent))
    // Reports has real screens now, so it is listed as included, not planned.
    expect(within(included).getByText('Reports and role dashboards, with CSV export')).toBeInTheDocument()
    expect(within(planned).queryByText(/report/i)).not.toBeInTheDocument()
    expect(within(planned).getByText('AI assistance beyond slot suggestions')).toBeInTheDocument()
    expect(within(included).queryByText('AI assistance')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Talk to us/ })).toHaveAttribute('href', '/contact')
  })
})

describe('pricing page: doctors', () => {
  it('lists the doctor directory and open slots, not availability management', () => {
    renderPublic(<PricingPage />)

    const included = screen.getByRole('region', { name: 'Included today' })
    expect(
      within(included).getByText('Doctor directory by department, with open slots when booking'),
    ).toBeInTheDocument()
    expect(within(included).queryByText('Doctors, departments, and availability')).not.toBeInTheDocument()
    expectNoScheduleClaims(pageText())
  })
})

describe('legal and security pages', () => {
  it.each(Object.values(legalDocs))('$title makes no unsupported claim', (doc) => {
    renderPublic(<LegalPage doc={doc} />)

    expectHonest(pageText())
  })

  it('the security page lists implemented controls and disclaims the rest', () => {
    renderPublic(<LegalPage doc={legalDocs.security} />)

    expect(screen.getByRole('heading', { level: 1, name: 'Security' })).toBeInTheDocument()
    expect(screen.getByText(/Argon2id/)).toBeInTheDocument()
    expect(
      screen.getByText(
        /We do not currently claim HIPAA compliance, hold a SOC 2 report, or offer a Business Associate Agreement/,
      ),
    ).toBeInTheDocument()
    // The footer no longer advertises compliance.
    expect(screen.getByRole('link', { name: 'Security' })).toHaveAttribute('href', '/security')
    expect(screen.queryByRole('link', { name: /HIPAA/ })).not.toBeInTheDocument()
  })

  it('the terms describe administrative software, not clinical decision support', () => {
    renderPublic(<LegalPage doc={legalDocs.terms} />)

    expect(screen.getByText(/does not diagnose, recommend treatment, or make clinical decisions/)).toBeInTheDocument()
    expect(
      screen.getByText(/are records and not clinical advice\.$/),
    ).toHaveTextContent(/prescriptions it stores, and the laboratory results it marks/)
  })

  it('the terms describe the product that exists today, not the first three modules', () => {
    renderPublic(<LegalPage doc={legalDocs.terms} />)

    const service = screen.getByRole('heading', { name: 'The service' }).parentElement as HTMLElement
    for (const part of [
      'patient registration and records',
      'a directory of doctors and departments',
      'appointment scheduling',
      'billing',
      'laboratory orders and results',
      'pharmacy prescribing and dispensing',
      'inventory of supplies',
      'in-app notifications',
      'user and role management',
      'hospital settings',
      'an audit log',
    ]) {
      expect(service).toHaveTextContent(part)
    }
    expect(pageText()).not.toMatch(/patient registration, appointment scheduling, and billing\./)
    // Neither planned module is described as part of the service.
    expect(service.textContent).not.toMatch(/report|analytics|\bAI\b|assist/i)
  })

  it.each(Object.values(legalDocs))('$title claims no schedule management and plans no built module', (doc) => {
    const passages = [doc.intro, ...doc.sections.flatMap((section) => section.body)]

    expectNoScheduleClaims(passages.join(' '))
    expectBuiltModulesNotPlanned(passages)
  })
})

describe('contact page', () => {
  function fill(label: RegExp, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }

  it('hands the message to the email app and reports no success of its own', async () => {
    const user = userEvent.setup()
    renderPublic(<ContactPage />)
    fill(/^Name/, 'Asha Verma')
    fill(/Work email/, 'asha@example.org')
    fill(/Organization/, 'City Hospital')
    fill(/How can we help/, 'We would like to see the booking flow.')

    await user.click(screen.getByRole('button', { name: /Compose email/ }))

    await waitFor(() => expect(openEmailApp).toHaveBeenCalledTimes(1))
    expect(openEmailApp).toHaveBeenCalledWith(
      mailtoUrl({
        to: 'sales@aetheris.health',
        subject: 'Aetheris enquiry from City Hospital',
        body: 'We would like to see the booking flow.\n\nAsha Verma\nCity Hospital\nasha@example.org',
      }),
    )
    // Nothing was sent by the page, so it does not say anything was.
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(screen.queryByText(/we'll be in touch/i)).not.toBeInTheDocument()
    expect(fake.sent).toHaveLength(0)
    expect(screen.getByText(/Nothing is sent until you send it/)).toBeInTheDocument()
  })

  it('does not open the email app for an incomplete form', async () => {
    const user = userEvent.setup()
    renderPublic(<ContactPage />)

    await user.click(screen.getByRole('button', { name: /Compose email/ }))

    expect(await screen.findByText('Enter your name')).toBeInTheDocument()
    expect(openEmailApp).not.toHaveBeenCalled()
  })

  it('offers each address as a real mail link and makes no unsupported claim', () => {
    renderPublic(<ContactPage />)

    expect(screen.getByRole('link', { name: 'sales@aetheris.health' })).toHaveAttribute(
      'href',
      'mailto:sales@aetheris.health',
    )
    expectHonest(pageText())
  })
})

describe('dead controls', () => {
  function renderApp(page: React.ReactNode, permissions: string[] = []) {
    signIn(permissions)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>{page}</QueryClientProvider>
      </MemoryRouter>,
    )
  }

  it('Patients has no Export button, and keeps Register for those who may', async () => {
    renderApp(<PatientsPage />, ['patient.read', 'patient.create'])

    expect(await screen.findByRole('heading', { name: 'Patients' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /export/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Register Patient/ })).toBeInTheDocument()
  })

  it('Patients still hides Register from a user without patient.create', async () => {
    renderApp(<PatientsPage />, ['patient.read'])

    expect(await screen.findByRole('heading', { name: 'Patients' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Register Patient/ })).not.toBeInTheDocument()
  })

  it('sign-in has no "Remember me" box, which never did anything', () => {
    renderApp(<LoginPage />)

    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByText(/remember me/i)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Forgot password?' })).toHaveAttribute('href', '/forgot-password')
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  const reportLinks = () =>
    within(screen.getByRole('list', { name: 'Reports' }))
      .getAllByRole('link')
      .map((link) => [link.getAttribute('aria-label'), link.getAttribute('href')])

  it('Reports lists all four reports for an administrator, and no longer says it is unavailable', () => {
    renderApp(<ReportsPage />, ['report.admin.read'])

    expect(screen.getByRole('heading', { level: 1, name: 'Reports' })).toBeInTheDocument()
    expect(reportLinks()).toEqual([
      ['Patients report', '/reports/patients'],
      ['Appointments report', '/reports/appointments'],
      ['Revenue report', '/reports/revenue'],
      ['Outstanding report', '/reports/outstanding'],
    ])
    expect(pageText()).not.toMatch(/not available yet|planned|scaffolded|Spec Part/i)
  })

  it('Reports lists only Revenue and Outstanding for billing staff', () => {
    renderApp(<ReportsPage />, ['report.billing.read'])

    expect(screen.getByRole('heading', { level: 1, name: 'Reports' })).toBeInTheDocument()
    expect(reportLinks()).toEqual([
      ['Revenue report', '/reports/revenue'],
      ['Outstanding report', '/reports/outstanding'],
    ])
    expect(pageText()).not.toMatch(/not available yet|planned/i)
  })
})
