import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AxiosError } from 'axios'
import { bodyOf, fail, installFakeApi, ok, type FakeApi, type Outcome } from '@/test/fakeApi'
import { RegisterPatientDialog } from './RegisterPatientDialog'

/**
 * What happens when a registration is sent, against `POST /api/v1/patients`
 * (`backend/app/api/v1/patients.py`). The real `useCreatePatient` hook, `http`
 * wrapper and Axios instance run; only the network adapter is replaced, so the
 * number of requests counted here is the number that would leave the browser.
 * Every POST creates a patient and allocates an MRN — the endpoint takes no
 * idempotency key — so a second request is a duplicate record.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))

vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const created = { id: 'p1', mrn: 'MRN-2026-00044', full_name: 'Ananya Rao' }

let fake: FakeApi
let onPost: () => Outcome | Promise<Outcome>

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  onPost = () => ok(created, 201)
  fake = installFakeApi((config) => (config.method === 'post' ? onPost() : ok(null)))
})

afterEach(() => fake.restore())

const posts = () => fake.requests('post', '/patients')

async function openAndFill(user: ReturnType<typeof userEvent.setup>) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <RegisterPatientDialog trigger={<button type="button">Register Patient</button>} />
    </QueryClientProvider>,
  )
  await user.click(screen.getByRole('button', { name: 'Register Patient' }))
  const dialog = await screen.findByRole('dialog')
  await user.type(screen.getByLabelText(/First name/), 'Ananya')
  await user.type(screen.getByLabelText(/Last name/), 'Rao')
  await user.type(screen.getByLabelText(/Date of birth/), '1988-03-14')
  await user.click(screen.getByRole('combobox', { name: /Gender/ }))
  await user.click(await screen.findByRole('option', { name: 'Female' }))
  return dialog
}

const submit = (user: ReturnType<typeof userEvent.setup>) =>
  user.click(screen.getByRole('button', { name: 'Register' }))

describe('RegisterPatientDialog — sending', () => {
  it('creates one patient when the form is submitted several times in the same tick', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onPost = () => new Promise<Outcome>((resolve) => (finish = resolve))
    const user = userEvent.setup()
    const dialog = await openAndFill(user)

    // A double click, or Enter held down: submits fired before React can
    // re-render the button as disabled.
    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)
    fireEvent.submit(form)

    await waitFor(() => expect(posts()).toHaveLength(1))
    const busy = screen.getByRole('button', { name: 'Register' })
    await waitFor(() => expect(busy).toBeDisabled())
    await user.click(busy)

    finish(ok(created, 201))
    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('Registered Ananya Rao · MRN-2026-00044'),
    )
    expect(toastSuccess).toHaveBeenCalledTimes(1)
    expect(posts()).toHaveLength(1)
    expect(bodyOf(posts()[0])).toEqual({
      first_name: 'Ananya',
      last_name: 'Rao',
      date_of_birth: '1988-03-14',
      gender: 'female',
    })
  })

  it('can be submitted again after a failed attempt', async () => {
    onPost = () => ({ status: 500, data: 'Internal Server Error' })
    const user = userEvent.setup()
    await openAndFill(user)
    await submit(user)
    await waitFor(() => expect(toastError).toHaveBeenCalledTimes(1))

    onPost = () => ok(created, 201)
    await submit(user)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(2)
  })

  it('can be submitted again if preparing the request throws', async () => {
    const user = userEvent.setup()
    await openAndFill(user)

    // The date of birth is checked against the clock twice: by the form, then
    // again when the request is built. Fail only the second check, as a clock
    // correction between the two would.
    const realSetFullYear = Date.prototype.setFullYear
    let checks = 0
    const clock = vi.spyOn(Date.prototype, 'setFullYear').mockImplementation(function (
      this: Date,
      year: number,
    ) {
      checks += 1
      return realSetFullYear.call(this, checks === 2 ? year + 500 : year)
    })
    await submit(user)
    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not register the patient. Please try again.'),
    )
    expect(posts()).toHaveLength(0)
    clock.mockRestore()

    await submit(user)
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(posts()).toHaveLength(1)
  })

  it('shows a plain sentence, not transport text, for a server failure', async () => {
    onPost = () => ({ status: 500, data: 'Internal Server Error' })
    const user = userEvent.setup()
    await openAndFill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not register the patient. Please try again.'),
    )
  })

  it('shows a plain sentence when the network is down', async () => {
    onPost = () => {
      throw new AxiosError('Network Error', 'ERR_NETWORK')
    }
    const user = userEvent.setup()
    await openAndFill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('Could not register the patient. Please try again.'),
    )
  })

  it("shows the API's own sentence for a refusal it explains", async () => {
    onPost = () => fail(403, 'You do not have permission to register patients.')
    const user = userEvent.setup()
    await openAndFill(user)
    await submit(user)

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith('You do not have permission to register patients.'),
    )
  })

  it('puts a 422 under the field it names and the rest above the form', async () => {
    onPost = () =>
      fail(422, 'Validation failed.', {
        error_code: 'VALIDATION_ERROR',
        errors: [
          { field: 'phone', message: 'Value error, Phone number is not valid for this region' },
          { field: 'address.country', message: 'Country is required' },
        ],
      })
    const user = userEvent.setup()
    const dialog = await openAndFill(user)
    await submit(user)

    expect(
      await within(dialog).findByText('Phone number is not valid for this region'),
    ).toBeInTheDocument()
    expect(screen.getByLabelText(/Phone/)).toHaveAttribute('aria-invalid', 'true')
    expect(within(dialog).getByText('Country is required')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })
})
