import type { ReactNode } from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ManagedUser, RoleSummary } from '@/api/users'
import { MOCK_PERMISSIONS_BY_ROLE } from '@/lib/rbac'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import { InviteUserDialog } from './InviteUserDialog'
import { ManageRolesDialog } from './ManageRolesDialog'

/**
 * A token an older API build would still send with a new invite. Nothing the
 * dialog renders, copies or keeps may contain it.
 */
const LEGACY_TOKEN = 'tok-abc123-never-shown'

const QUEUED_TEXT =
  'new.nurse@hospital.test has been added with Invited status. An invitation email has been queued for this address. Delivery is not confirmed. If it does not arrive, use Resend invitation on the Users page.'
const UNAVAILABLE_TEXT =
  'new.nurse@hospital.test has been added with Invited status, but no invitation email was sent because email delivery is not available. The user cannot set a password until an invitation is sent. Use Resend invitation on the Users page once email delivery is working.'

/**
 * Role assignment and invites against `POST /users`, `POST /users/{id}/roles`
 * and `DELETE /users/{id}/roles/{role_id}` (`backend/app/api/v1/users.py`).
 * The real hooks, permission store and Axios instance run; only the network
 * adapter is replaced.
 *
 * The rule under test is `_assert_grantable`
 * (`backend/app/services/user_service.py`): a role that includes a permission
 * the actor does not hold is refused with a 403.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const SUPER: RoleSummary = { id: 'r-super', name: 'Super Admin', description: null, is_system: true }
const HOSPITAL: RoleSummary = { id: 'r-hadmin', name: 'Hospital Admin', description: null, is_system: true }
const NURSE: RoleSummary = { id: 'r-nurse', name: 'Nurse', description: null, is_system: true }
/** A hospital's own role: the frontend cannot know what it grants. */
const CUSTOM: RoleSummary = { id: 'r-custom', name: 'Ward Clerk', description: null, is_system: false }

const target: ManagedUser = {
  id: 'u2',
  email: 'priya@hospital.test',
  first_name: 'Priya',
  last_name: 'Nair',
  phone: null,
  status: 'active',
  hospital_id: 'h1',
  roles: [HOSPITAL],
  mfa_enabled: false,
  last_login_at: null,
  password_changed_at: null,
  created_at: null,
  updated_at: null,
}

/** The account `POST /users` creates. */
const invited: ManagedUser = { ...target, id: 'u9', email: 'new.nurse@hospital.test', status: 'invited' }

const ESCALATION = 'You cannot grant a role that includes permissions you do not hold.'

let fake: FakeApi
let onAssign: () => Outcome | Promise<Outcome>
let onRemove: () => Outcome | Promise<Outcome>
let onInvite: () => Outcome | Promise<Outcome>

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  onAssign = () => ok(null)
  onRemove = () => ok(null)
  onInvite = () => ok({ ...invited, invitation: { delivery: 'queued' } }, 201)
  fake = installFakeApi((config) => {
    if (config.method === 'get' && config.url === '/roles') return paged([SUPER, HOSPITAL, NURSE, CUSTOM])
    if (config.method === 'get' && config.url === '/users/u2') return ok(target)
    if (config.method === 'post' && config.url === '/users/u2/roles') return onAssign()
    if (config.method === 'delete') return onRemove()
    if (config.method === 'post' && config.url === '/users') return onInvite()
    return paged([])
  })
})

afterEach(() => {
  fake.restore()
  signOut()
})

function withClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
}

const assigns = () => fake.requests('post', '/users/u2/roles')
const invites = () => fake.sent.filter((c) => c.method === 'post' && c.url === '/users')

async function offeredRoles(user: ReturnType<typeof userEvent.setup>, trigger: HTMLElement) {
  await user.click(trigger)
  const names = (await screen.findAllByRole('option')).map((o) => o.textContent)
  await user.keyboard('{Escape}')
  return names
}

describe('ManageRolesDialog', () => {
  function renderDialog() {
    withClient(<ManageRolesDialog user={target} onClose={() => {}} />)
  }

  const roleSelect = () => screen.findByRole('combobox', { name: 'Assign a role' })

  async function pick(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
    await user.click(await roleSelect())
    await user.click(await screen.findByRole('option', { name }))
  }

  it('does not offer a Hospital Admin the Super Admin role, which the API would refuse', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const user = userEvent.setup()
    renderDialog()
    await waitFor(() => expect(fake.requests('get', '/roles')).toHaveLength(1))

    const names = await offeredRoles(user, await roleSelect())
    expect(names).toEqual(['Nurse (system)', 'Ward Clerk'])
  })

  it('offers Super Admin to someone who holds everything it grants', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.super_admin)
    const user = userEvent.setup()
    renderDialog()
    await waitFor(() => expect(fake.requests('get', '/roles')).toHaveLength(1))

    expect(await offeredRoles(user, await roleSelect())).toContain('Super Admin (system)')
  })

  it("reports the API's reason when an assignment is refused", async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onAssign = () => fail(403, ESCALATION, { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    renderDialog()
    await pick(user, /Ward Clerk/)
    await user.click(screen.getByRole('button', { name: /Assign/ }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith(ESCALATION))
  })

  it('does not blame a permission, or show server text, when the server fails', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onAssign = () => ({ status: 500, data: 'Internal Server Error' })
    const user = userEvent.setup()
    renderDialog()
    await pick(user, /Ward Clerk/)
    await user.click(screen.getByRole('button', { name: /Assign/ }))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't assign the role. Please try again."),
    )
  })

  it('sends one assignment when Assign is clicked twice before the button disables', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    let finish: (outcome: Outcome) => void = () => {}
    onAssign = () => new Promise<Outcome>((resolve) => (finish = resolve))
    const user = userEvent.setup()
    renderDialog()
    await pick(user, /Nurse/)

    const assign = screen.getByRole('button', { name: /Assign/ })
    // Two clicks dispatched back to back, with no re-render in between.
    assign.click()
    assign.click()

    await waitFor(() => expect(assigns()).toHaveLength(1))
    finish(ok(null))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(assigns()).toHaveLength(1)
    expect(bodyOf(assigns()[0])).toEqual({ role_id: 'r-nurse' })
  })

  it('sends one removal when the remove button is clicked twice', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    let finish: (outcome: Outcome) => void = () => {}
    onRemove = () => new Promise<Outcome>((resolve) => (finish = resolve))
    renderDialog()

    const removeButton = await screen.findByRole('button', { name: 'Remove Hospital Admin' })
    removeButton.click()
    removeButton.click()

    await waitFor(() => expect(fake.requests('delete')).toHaveLength(1))
    finish(ok(null))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(fake.requests('delete')).toHaveLength(1)
  })

  it("says why the API refused to remove the last administrator's role", async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const reason =
      "This is the hospital's last administrator — assign the role to someone else before removing it."
    onRemove = () => fail(400, reason, { error_code: 'BUSINESS_RULE_VIOLATION' })
    const user = userEvent.setup()
    renderDialog()

    await user.click(await screen.findByRole('button', { name: 'Remove Hospital Admin' }))
    await waitFor(() => expect(toastError).toHaveBeenCalledWith(reason))
  })
})

describe('InviteUserDialog', () => {
  async function openDialog(user: ReturnType<typeof userEvent.setup>) {
    withClient(<InviteUserDialog trigger={<button type="button">Invite user</button>} />)
    await user.click(screen.getByRole('button', { name: 'Invite user' }))
    return screen.findByRole('dialog')
  }

  async function fill(user: ReturnType<typeof userEvent.setup>) {
    await user.type(screen.getByLabelText(/Email/), 'new.nurse@hospital.test')
    await user.type(screen.getByLabelText(/First name/), 'Meera')
    await user.type(screen.getByLabelText(/Last name/), 'Iyer')
  }

  const submit = (user: ReturnType<typeof userEvent.setup>) =>
    user.click(screen.getByRole('button', { name: 'Create invite' }))

  /** Every place a link could be put in front of the admin. */
  function expectNoActivationLink() {
    expect(screen.queryByRole('textbox', { name: /link/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /copy/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    const page = document.documentElement.outerHTML
    expect(page).not.toContain('token=')
    expect(page).not.toContain('/reset-password')
  }

  it('says the link goes to the user by email and is not shown to the admin', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const dialog = await openDialog(userEvent.setup())

    expect(
      within(dialog).getByText(
        'Creates the account with Invited status. The user sets a password from a one-time link sent to their email address. The link is not shown here.',
      ),
    ).toBeInTheDocument()
  })

  it('reports a queued invitation email without claiming it was delivered', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)
    await submit(user)

    expect(await within(dialog).findByRole('heading', { name: 'Invite created' })).toBeInTheDocument()
    expect(within(dialog).getByText(QUEUED_TEXT)).toBeInTheDocument()
    expect(dialog).toHaveAccessibleDescription(QUEUED_TEXT)
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
    expect(toastSuccess).toHaveBeenCalledWith('Invite created for new.nurse@hospital.test')
    expectNoActivationLink()

    await user.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('warns that no email was sent when email delivery is unavailable', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onInvite = () => ok({ ...invited, invitation: { delivery: 'unavailable' } }, 201)
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)
    await submit(user)

    expect(
      await within(dialog).findByRole('heading', { name: 'Invite created, email not sent' }),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('alert')).toHaveTextContent(UNAVAILABLE_TEXT)
    expect(dialog).toHaveAccessibleDescription(UNAVAILABLE_TEXT)
    expect(within(dialog).queryByText(/has been queued/)).not.toBeInTheDocument()
    // Nothing was sent, so nothing announces success.
    expect(toastSuccess).not.toHaveBeenCalled()
    expectNoActivationLink()

    await user.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it.each([
    ['is missing', {}],
    ['is null', { invitation: null }],
    ['has no delivery', { invitation: {} }],
    ['names a delivery this build does not know', { invitation: { delivery: 'sent' } }],
  ])('treats the invite as not emailed when the invitation result %s', async (_case, extra) => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onInvite = () => ok({ ...invited, ...extra }, 201)
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)
    await submit(user)

    expect(
      await within(dialog).findByRole('heading', { name: 'Invite created, email not sent' }),
    ).toBeInTheDocument()
    expect(within(dialog).getByRole('alert')).toHaveTextContent(UNAVAILABLE_TEXT)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it.each([
    ['queued', { delivery: 'queued' }],
    ['unavailable', { delivery: 'unavailable' }],
    ['missing', undefined],
  ])(
    'never shows, copies or keeps a token an older API still sends (invitation %s)',
    async (_case, invitation) => {
      signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
      onInvite = () => ok({ ...invited, invitation, invite_token: LEGACY_TOKEN }, 201)
      const user = userEvent.setup()
      const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(
        <QueryClientProvider client={client}>
          <InviteUserDialog trigger={<button type="button">Invite user</button>} />
        </QueryClientProvider>,
      )
      await user.click(screen.getByRole('button', { name: 'Invite user' }))
      await fill(user)
      await submit(user)
      await screen.findByRole('button', { name: 'Done' })

      expect(document.documentElement.outerHTML).not.toContain(LEGACY_TOKEN)
      expect(document.documentElement.outerHTML).not.toContain('tok-')
      expectNoActivationLink()
      // Not in what a toast said, on the clipboard, or in the mutation cache.
      expect(JSON.stringify([toastSuccess.mock.calls, toastError.mock.calls])).not.toContain('tok-')
      expect(await navigator.clipboard.readText()).toBe('')
      const held = client.getMutationCache().getAll().map((m) => m.state.data)
      expect(held).toHaveLength(1)
      expect(JSON.stringify(held)).not.toContain('tok-')
      expect(JSON.stringify(held)).not.toContain('invite_token')
    },
  )

  it('closes the result on Escape: there is nothing on it to lose', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const user = userEvent.setup()
    await openDialog(user)
    await fill(user)
    await submit(user)
    await screen.findByRole('heading', { name: 'Invite created' })

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('still closes the empty form on Escape', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const user = userEvent.setup()
    await openDialog(user)

    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends one invite when the form is submitted twice in the same tick', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    let finish: (outcome: Outcome) => void = () => {}
    onInvite = () => new Promise<Outcome>((resolve) => (finish = resolve))
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)

    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    await waitFor(() => expect(invites()).toHaveLength(1))
    finish(ok({ ...invited, invitation: { delivery: 'queued' } }, 201))
    await screen.findByRole('heading', { name: 'Invite created' })
    expect(invites()).toHaveLength(1)
  })

  it('names each role row and its remove button, and offers only grantable roles', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    const user = userEvent.setup()
    await openDialog(user)
    await waitFor(() => expect(screen.getByRole('button', { name: /Add role/ })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: /Add role/ }))
    await user.click(screen.getByRole('button', { name: /Add role/ }))

    expect(screen.getByRole('combobox', { name: /Assign role/ })).toBeInTheDocument()
    const second = screen.getByRole('combobox', { name: 'Role 2' })
    expect(screen.getByRole('button', { name: 'Remove role 1' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove role 2' })).toBeInTheDocument()

    expect(await offeredRoles(user, second)).toEqual(['Hospital Admin', 'Nurse', 'Ward Clerk'])
  })

  it('puts a 422 under the field it names and the rest above the form', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onInvite = () =>
      fail(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: [
          { field: 'email', message: 'value is not a valid email address' },
          { field: 'role_ids.0', message: 'Input should be a valid UUID' },
          { field: 'department_id', message: 'Extra inputs are not permitted' },
        ],
      })
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)
    await waitFor(() => expect(screen.getByRole('button', { name: /Add role/ })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: /Add role/ }))
    await user.click(screen.getByRole('combobox', { name: /Assign role/ }))
    await user.click(await screen.findByRole('option', { name: 'Nurse' }))
    await submit(user)

    expect(await within(dialog).findByText('value is not a valid email address')).toBeInTheDocument()
    expect(within(dialog).getByText('Input should be a valid UUID')).toBeInTheDocument()
    expect(within(dialog).getByText('Extra inputs are not permitted')).toBeInTheDocument()
    expect(screen.getByLabelText(/Email/)).toHaveAttribute('aria-invalid', 'true')
    expect(toastError).toHaveBeenCalledWith('Extra inputs are not permitted')
  })

  it("reports the API's reason for a refusal instead of asking for a retry", async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onInvite = () => fail(403, ESCALATION, { error_code: 'PERMISSION_DENIED' })
    const user = userEvent.setup()
    const dialog = await openDialog(user)
    await fill(user)
    await submit(user)

    expect(await within(dialog).findByText(ESCALATION)).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledWith(ESCALATION)
  })

  it('shows a plain sentence, not server text, when the server fails', async () => {
    signIn(MOCK_PERMISSIONS_BY_ROLE.hospital_admin)
    onInvite = () => ({ status: 502, data: '<html>Bad Gateway</html>' })
    const user = userEvent.setup()
    await openDialog(user)
    await fill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't create the invite. Please try again."),
    )
  })
})
