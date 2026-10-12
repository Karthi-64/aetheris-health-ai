import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import { doctorKeys, type Doctor, type DoctorSummary, type UpdateDoctorInput } from '@/api/doctors'
import type { DepartmentSummary } from '@/api/departments'
import { formatMoney } from '@/lib/format'
import { signIn, signOut } from '@/test/auth'
import { bodyOf, fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import DoctorDetailPage from './DoctorDetailPage'
import DoctorsPage from './DoctorsPage'

/**
 * A doctor's page and the three writes it offers — edit, deactivate,
 * reactivate — against the contract in `backend/app/schemas/doctor.py` and
 * `backend/app/api/v1/doctors.py`.
 *
 * The real hooks, permission check, `http` wrapper and Axios instance run
 * against an in-memory server that applies a write the way the API does and
 * refuses what the API refuses (an empty body, an unknown key, an inactive
 * department, an edit of a deactivated doctor). Each test asserts on the
 * request that would leave the browser and on what the page shows afterwards.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

// Role → doctor permissions as seeded (backend/app/seeds/seed.py): only a
// Hospital Admin can change a doctor; the Doctor role can read.
const ADMIN = ['doctor.read', 'doctor.update', 'doctor.delete', 'department.read']
const DOCTOR = ['doctor.read', 'department.read']

const EDITABLE = [
  'specialization',
  'license_number',
  'consultation_fee',
  'department_id',
  'qualifications',
  'languages',
  'bio',
]

function makeDoctor(overrides: Partial<Doctor> = {}): Doctor {
  return {
    id: 'doc-1',
    hospital_id: 'hosp-1',
    user_id: 'user-9',
    full_name: 'Priya Sharma',
    email: 'priya.sharma@example.com',
    specialization: 'Cardiology',
    license_number: 'MCI-48213',
    consultation_fee: '950.00',
    department_id: 'dep-cardio',
    department_name: 'Cardiology',
    qualifications: [
      { degree: 'MBBS', institution: 'AIIMS Delhi', year: 2008 },
      // Saved without the optional keys, so the API returns it without them.
      { degree: 'MD' },
    ],
    languages: ['English', 'Hindi'],
    bio: 'Interventional cardiologist.',
    status: 'active',
    created_at: '2026-01-05T04:00:00Z',
    updated_at: '2026-09-01T04:00:00Z',
    ...overrides,
  }
}

const makeDepartments = (): DepartmentSummary[] => [
  { id: 'dep-cardio', code: 'CARD', name: 'Cardiology', location: null, status: 'active' },
  { id: 'dep-neuro', code: 'NEUR', name: 'Neurology', location: 'Block B', status: 'active' },
]

let api: FakeApi
/** The record as the server holds it. */
let doctor: Doctor
let departments: DepartmentSummary[]
/** Override to refuse, change or delay the next write. */
let onWrite: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
/** Override the doctor read, e.g. to fail or hold it. */
let onRead: (() => Outcome | Promise<Outcome>) | null
/** Override the department list, e.g. to fail it. */
let onDepartments: (() => Outcome) | null

const invalid = (errors: Array<{ field: string; message: string }>) =>
  fail(422, 'Validation failed.', { error_code: 'VALIDATION_ERROR', errors })

const notFound = () =>
  fail(404, 'Doctor not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { doctor_id: 'doc-1' } })

const ruleBroken = (message: string) =>
  fail(400, message, { error_code: 'BUSINESS_RULE_VIOLATION', errors: { doctor_id: 'doc-1' } })

/** `PATCH /doctors/{id}` as the API applies it, in the API's order of checks. */
function patch(config: InternalAxiosRequestConfig): Outcome {
  const body = (bodyOf(config) ?? {}) as Record<string, unknown>
  const unknown = Object.keys(body).filter((key) => !EDITABLE.includes(key))
  if (unknown.length > 0) {
    return invalid(unknown.map((field) => ({ field, message: 'Extra inputs are not permitted' })))
  }
  if (Object.keys(body).length === 0) {
    return invalid([{ field: '', message: 'Value error, Update request must contain at least one field.' }])
  }
  // A deactivated doctor cannot be edited.
  if (doctor.status === 'inactive') return notFound()

  const change = body as UpdateDoctorInput
  if (change.department_id && !departments.some((d) => d.id === change.department_id && d.status === 'active')) {
    return fail(422, 'Department not found in this hospital.', {
      error_code: 'VALIDATION_ERROR',
      errors: { errors: [{ field: 'department_id', message: 'Unknown department.' }] },
    })
  }

  Object.assign(doctor, change)
  if ('department_id' in change) {
    doctor.department_name = departments.find((d) => d.id === change.department_id)?.name ?? null
  }
  if (change.consultation_fee !== undefined) {
    doctor.consultation_fee = Number(change.consultation_fee).toFixed(2)
  }
  if (change.languages) doctor.languages = change.languages.map((l) => l.trim()).filter(Boolean)
  doctor.updated_at = '2026-10-05T06:30:00Z'
  return ok(structuredClone(doctor))
}

function write(config: InternalAxiosRequestConfig): Outcome {
  const url = config.url ?? ''
  if (config.method === 'patch' && url === '/doctors/doc-1') return patch(config)
  if (config.method === 'delete' && url === '/doctors/doc-1') {
    if (doctor.status === 'inactive') return ruleBroken('Doctor is already deactivated.')
    doctor.status = 'inactive'
    return ok(structuredClone(doctor))
  }
  if (config.method === 'post' && url === '/doctors/doc-1/activate') {
    if (doctor.status === 'active') return ruleBroken('Doctor is already active.')
    doctor.status = 'active'
    return ok(structuredClone(doctor))
  }
  return fail(404, 'Not Found', { error_code: 'RESOURCE_NOT_FOUND', errors: null })
}

/** The doctor as `GET /doctors` lists it. */
function summary(): DoctorSummary {
  const { id, user_id, full_name, specialization, department_id, department_name, consultation_fee, status } =
    doctor
  return { id, user_id, full_name, specialization, department_id, department_name, consultation_fee, status }
}

function read(config: InternalAxiosRequestConfig): Outcome | Promise<Outcome> {
  const url = config.url ?? ''
  // A deactivated doctor is hidden unless the caller asks for it.
  const hidden = doctor.status === 'inactive' && config.params?.include_inactive !== true
  if (url === '/doctors/doc-1') {
    if (onRead) return onRead()
    return hidden ? notFound() : ok(structuredClone(doctor))
  }
  if (url === '/doctors') return paged(hidden ? [] : [summary()])
  // Only active departments are listed.
  if (url === '/departments') {
    return onDepartments ? onDepartments() : paged(departments.filter((d) => d.status === 'active'))
  }
  return fail(404, 'Not Found', { error_code: 'RESOURCE_NOT_FOUND', errors: null })
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  doctor = makeDoctor()
  departments = makeDepartments()
  onWrite = null
  onRead = null
  onDepartments = null
  api = installFakeApi((config) =>
    config.method === 'get' ? read(config) : (onWrite ?? write)(config),
  )
})

afterEach(() => {
  // Unmount before signing out, so no mounted component reacts to it.
  cleanup()
  api.restore()
  signOut()
})

/** Open the app on the doctor's page, or `at` the directory that links to it. */
function renderDoctor(permissions: string[], at = '/doctors/doc-1') {
  signIn(permissions)
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <MemoryRouter initialEntries={[at]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/doctors" element={<DoctorsPage />} />
          <Route path="/doctors/:doctorId" element={<DoctorDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
  return client
}

type User = ReturnType<typeof userEvent.setup>

const pageLoaded = () => screen.findByRole('heading', { name: 'Priya Sharma' })
const writes = () => api.sent.filter((c) => c.method !== 'get')
const doctorReads = () => api.sent.filter((c) => c.method === 'get' && c.url === '/doctors/doc-1')
const listReads = () => api.sent.filter((c) => c.method === 'get' && c.url === '/doctors')
const urlOf = (config: InternalAxiosRequestConfig) => `${config.baseURL}${config.url}`

/** Wait for the doctor, then return the labels of the actions the page offers. */
async function actions() {
  await pageLoaded()
  const group = screen.queryByRole('group', { name: 'Doctor actions' })
  return group ? within(group).getAllByRole('button').map((b) => b.textContent?.trim()) : []
}

const action = (name: string) =>
  within(screen.getByRole('group', { name: 'Doctor actions' })).getByRole('button', { name })

async function openAction(user: User, name: string) {
  await pageLoaded()
  await user.click(action(name))
  return screen.findByRole('dialog')
}

/** The block on the page holding a labelled value. Works behind an open dialog too. */
const shown = (label: string) => screen.getByText(label, { selector: 'p' }).parentElement as HTMLElement

const input = (dialog: HTMLElement, label: RegExp) => within(dialog).getByLabelText(label)
const department = (dialog: HTMLElement) => within(dialog).getByRole('combobox', { name: /Department/ })
const qualification = (dialog: HTMLElement, n: number) =>
  within(dialog).getByRole('listitem', { name: `Qualification ${n}` })
const saveButton = (dialog: HTMLElement) => within(dialog).getByRole('button', { name: 'Save changes' })

async function retype(user: User, field: HTMLElement, value: string) {
  await user.clear(field)
  if (value) await user.type(field, value)
}

async function chooseDepartment(user: User, dialog: HTMLElement, name: string) {
  await user.click(department(dialog))
  await user.click(await screen.findByRole('option', { name }))
}

/** Open the department select and wait for it to offer exactly these. */
async function departmentOptions(user: User, dialog: HTMLElement, expected: string[]) {
  await user.click(department(dialog))
  await waitFor(() =>
    expect(screen.getAllByRole('option').map((o) => o.textContent)).toEqual(expected),
  )
  await user.keyboard('{Escape}')
}

/**
 * Let what is already under way finish: a response on its way in, the cache
 * telling its observers, a dialog handing the focus back.
 */
const settle = () =>
  act(async () => {
    for (let turn = 0; turn < 3; turn += 1) await new Promise((resolve) => setTimeout(resolve, 0))
  })

/** Save, wait for the request to succeed, and return it. */
async function saved(user: User, dialog: HTMLElement) {
  await user.click(saveButton(dialog))
  await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor updated'))
  expect(writes()).toHaveLength(1)
  return writes()[0]
}

describe('doctor detail', () => {
  it('asks for the doctor including inactive ones, and shows the record', async () => {
    renderDoctor(DOCTOR)

    await pageLoaded()
    const [request] = doctorReads()
    expect(urlOf(request)).toBe('/api/v1/doctors/doc-1')
    expect(request.params).toEqual({ include_inactive: true })

    expect(shown('Department')).toHaveTextContent('Cardiology')
    expect(shown('Licence')).toHaveTextContent('MCI-48213')
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('950.00'))
    expect(shown('Languages')).toHaveTextContent('English, Hindi')
    expect(screen.getByText('MBBS (AIIMS Delhi, 2008) · MD')).toBeInTheDocument()
    expect(screen.getByText('Interventional cardiologist.')).toBeInTheDocument()
    expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument()
  })

  it('shows a loading state, then the doctor', async () => {
    let finish: () => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (finish = () => resolve(ok(doctor))))
    renderDoctor(DOCTOR)

    expect(await screen.findByLabelText('Loading doctor')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Priya Sharma' })).not.toBeInTheDocument()
    finish()
    expect(await pageLoaded()).toBeInTheDocument()
  })

  it('offers a retry when the doctor fails to load', async () => {
    onRead = () => fail(500, 'An unexpected error occurred.', { error_code: 'INTERNAL_ERROR' })
    const user = userEvent.setup()
    renderDoctor(DOCTOR)

    expect(await screen.findByText("Couldn't load this doctor")).toBeInTheDocument()
    onRead = null
    await user.click(screen.getByRole('button', { name: /Retry/ }))
    expect(await pageLoaded()).toBeInTheDocument()
  })

  it('opens a deactivated doctor and marks it inactive', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    renderDoctor(DOCTOR)

    // The server only returns this record because include_inactive was sent.
    await pageLoaded()
    expect(screen.getByText('This doctor is inactive')).toBeInTheDocument()
    expect(screen.getByText('inactive', { selector: 'span' })).toBeInTheDocument()
  })

  it.each([
    ['an admin', ADMIN, /can't be edited\. Reactivate the doctor to make them available again\.$/],
    ['a read-only user', DOCTOR, /can't be edited\.$/],
    // Reactivating needs doctor.update, not doctor.delete.
    ['a user who can only deactivate', ['doctor.read', 'doctor.delete'], /can't be edited\.$/],
  ])('ends the inactive notice for %s with no more than they can do about it', async (_who, permissions, ending) => {
    doctor = makeDoctor({ status: 'inactive' })
    renderDoctor(permissions)

    await pageLoaded()
    expect(screen.getByRole('alert')).toHaveTextContent('This doctor is inactive')
    expect(screen.getByRole('alert')).toHaveTextContent(ending)
  })
})

describe('doctor actions by status and permission', () => {
  it.each([
    ['an admin', ADMIN, 'active', ['Edit', 'Deactivate']],
    ['an admin', ADMIN, 'inactive', ['Reactivate']],
    ['a read-only user', DOCTOR, 'active', []],
    ['a read-only user', DOCTOR, 'inactive', []],
    ['a user who can only update', ['doctor.read', 'doctor.update'], 'active', ['Edit']],
    ['a user who can only update', ['doctor.read', 'doctor.update'], 'inactive', ['Reactivate']],
    ['a user who can only deactivate', ['doctor.read', 'doctor.delete'], 'active', ['Deactivate']],
    // Reactivating needs doctor.update, not doctor.delete.
    ['a user who can only deactivate', ['doctor.read', 'doctor.delete'], 'inactive', []],
  ] as const)('gives %s the right actions on an %s doctor', async (_who, permissions, status, expected) => {
    doctor = makeDoctor({ status })
    renderDoctor([...permissions])
    expect(await actions()).toEqual(expected)
  })
})

describe('the edit form', () => {
  it('opens filled with the record, with name and email read-only', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(input(dialog, /Specialization/)).toHaveValue('Cardiology')
    expect(input(dialog, /Licence number/)).toHaveValue('MCI-48213')
    expect(input(dialog, /Consultation fee/)).toHaveValue('950.00')
    expect(department(dialog)).toHaveTextContent('Cardiology')
    expect(input(dialog, /Languages/)).toHaveValue('English, Hindi')
    expect(input(dialog, /Bio/)).toHaveValue('Interventional cardiologist.')

    expect(within(dialog).getAllByRole('listitem')).toHaveLength(2)
    const first = within(qualification(dialog, 1))
    expect(first.getByLabelText(/Degree/)).toHaveValue('MBBS')
    expect(first.getByLabelText(/Institution/)).toHaveValue('AIIMS Delhi')
    expect(first.getByLabelText(/Year/)).toHaveValue('2008')
    const second = within(qualification(dialog, 2))
    expect(second.getByLabelText(/Degree/)).toHaveValue('MD')
    expect(second.getByLabelText(/Institution/)).toHaveValue('')
    expect(second.getByLabelText(/Year/)).toHaveValue('')

    // The linked user account owns these; the API has no key for either.
    expect(within(dialog).getByText('Priya Sharma')).toBeInTheDocument()
    expect(within(dialog).getByText('priya.sharma@example.com')).toBeInTheDocument()
    expect(within(dialog).getByText(/belong to the doctor's user account/)).toBeInTheDocument()
    expect(within(dialog).queryByLabelText(/Name/)).not.toBeInTheDocument()
    expect(within(dialog).queryByLabelText(/Email/)).not.toBeInTheDocument()
    expect(within(dialog).queryByDisplayValue('Priya Sharma')).not.toBeInTheDocument()
  })

  it('offers the active departments and "Unassigned"', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await departmentOptions(user, dialog, ['Unassigned', 'Cardiology', 'Neurology'])
    expect(saveButton(dialog)).toBeDisabled()
  })

  it('still opens, on the current department, when the department list cannot be loaded', async () => {
    onDepartments = () => fail(500, 'An unexpected error occurred.', { error_code: 'INTERNAL_ERROR' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(await within(dialog).findByText("The department list couldn't be loaded.")).toBeInTheDocument()
    expect(department(dialog)).toHaveTextContent('Cardiology')
    // Not known to be inactive: the list that would say so never arrived.
    await departmentOptions(user, dialog, ['Unassigned', 'Cardiology'])

    await retype(user, input(dialog, /Consultation fee/), '1200')
    expect(bodyOf(await saved(user, dialog))).toEqual({ consultation_fee: '1200.00' })
  })

  it('keeps Save disabled until something changes, and again once it is undone', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(saveButton(dialog)).toBeDisabled()

    await user.type(input(dialog, /Specialization/), 'x')
    expect(saveButton(dialog)).toBeEnabled()
    await user.type(input(dialog, /Specialization/), '{Backspace}')
    expect(saveButton(dialog)).toBeDisabled()

    // The same for a list: a row added and taken away again is no change.
    await user.click(within(dialog).getByRole('button', { name: 'Add qualification' }))
    expect(saveButton(dialog)).toBeEnabled()
    await user.click(within(dialog).getByRole('button', { name: 'Remove qualification 3' }))
    expect(saveButton(dialog)).toBeDisabled()
    expect(writes()).toHaveLength(0)
  })
})

describe('saving an edit', () => {
  it('sends only the fee, as text, and shows the record the server returned', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200.5')
    const request = await saved(user, dialog)

    expect(request.method).toBe('patch')
    expect(urlOf(request)).toBe('/api/v1/doctors/doc-1')
    // Nothing but the changed key, and a decimal string rather than a number.
    expect(bodyOf(request)).toEqual({ consultation_fee: '1200.50' })
    expect(request.data).toBe('{"consultation_fee":"1200.50"}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('1200.50'))
    // What is shown is the PATCH response itself — no second read was needed.
    expect(doctorReads()).toHaveLength(1)
  })

  it('accepts a fee of zero', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '0')

    expect(bodyOf(await saved(user, dialog))).toEqual({ consultation_fee: '0.00' })
    await waitFor(() => expect(shown('Consultation fee')).toHaveTextContent(formatMoney('0.00')))
  })

  it('unassigns the department by sending null', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await chooseDepartment(user, dialog, 'Unassigned')

    expect(bodyOf(await saved(user, dialog))).toEqual({ department_id: null })
    await waitFor(() => expect(shown('Department')).toHaveTextContent('—'))
  })

  it('sends the id of the department chosen', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await chooseDepartment(user, dialog, 'Neurology')

    expect(bodyOf(await saved(user, dialog))).toEqual({ department_id: 'dep-neuro' })
    // The name is the server's: the request carried only the id.
    await waitFor(() => expect(shown('Department')).toHaveTextContent('Neurology'))
  })

  it('assigns a department to a doctor who had none', async () => {
    doctor = makeDoctor({ department_id: null, department_name: null })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(department(dialog)).toHaveTextContent('Unassigned')
    await chooseDepartment(user, dialog, 'Cardiology')

    expect(bodyOf(await saved(user, dialog))).toEqual({ department_id: 'dep-cardio' })
  })

  it('clears the bio by sending null, not an empty string', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.clear(input(dialog, /Bio/))

    expect(bodyOf(await saved(user, dialog))).toEqual({ bio: null })
    await waitFor(() => expect(screen.queryByText('Interventional cardiologist.')).not.toBeInTheDocument())
    expect(screen.queryByRole('heading', { name: 'About' })).not.toBeInTheDocument()
  })

  it('resends the whole qualification list when one is added', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.click(within(dialog).getByRole('button', { name: 'Add qualification' }))
    const added = within(qualification(dialog, 3))
    await user.type(added.getByLabelText(/Degree/), ' DM ')
    await user.type(added.getByLabelText(/Year/), '2015')

    // Every item, in the API's shape: blank optional keys left out, the year a number.
    expect(bodyOf(await saved(user, dialog))).toEqual({
      qualifications: [
        { degree: 'MBBS', institution: 'AIIMS Delhi', year: 2008 },
        { degree: 'MD' },
        { degree: 'DM', year: 2015 },
      ],
    })
    expect(await screen.findByText('MBBS (AIIMS Delhi, 2008) · MD · DM (2015)')).toBeInTheDocument()
  })

  it('resends what is left when a qualification is removed', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.click(within(dialog).getByRole('button', { name: 'Remove qualification 1' }))
    expect(within(dialog).getAllByRole('listitem')).toHaveLength(1)

    expect(bodyOf(await saved(user, dialog))).toEqual({ qualifications: [{ degree: 'MD' }] })
    expect(await screen.findByText('MD')).toBeInTheDocument()
  })

  it('clears the qualifications with an empty list', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.click(within(dialog).getByRole('button', { name: 'Remove qualification 2' }))
    await user.click(within(dialog).getByRole('button', { name: 'Remove qualification 1' }))
    expect(within(dialog).getByText('No qualifications listed.')).toBeInTheDocument()

    expect(bodyOf(await saved(user, dialog))).toEqual({ qualifications: [] })
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Qualifications' })).not.toBeInTheDocument())
  })

  it('sends an edited qualification with its institution and year', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    const second = within(qualification(dialog, 2))
    await user.type(second.getByLabelText(/Institution/), 'PGIMER Chandigarh')
    await user.type(second.getByLabelText(/Year/), '2012')

    expect(bodyOf(await saved(user, dialog))).toEqual({
      qualifications: [
        { degree: 'MBBS', institution: 'AIIMS Delhi', year: 2008 },
        { degree: 'MD', institution: 'PGIMER Chandigarh', year: 2012 },
      ],
    })
  })

  it('sends languages as a trimmed list without blanks', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(within(dialog).getByText(/Separate languages with commas/)).toBeInTheDocument()
    await retype(user, input(dialog, /Languages/), '  English ,, Tamil ,Hindi, ')

    expect(bodyOf(await saved(user, dialog))).toEqual({ languages: ['English', 'Tamil', 'Hindi'] })
    await waitFor(() => expect(shown('Languages')).toHaveTextContent('English, Tamil, Hindi'))
  })

  it('clears the languages with an empty list', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.clear(input(dialog, /Languages/))

    expect(bodyOf(await saved(user, dialog))).toEqual({ languages: [] })
  })

  it('sends every changed key, trimmed, and no others', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Specialization/), '  Interventional Cardiology ')
    await retype(user, input(dialog, /Licence number/), 'MCI-99001')
    await retype(user, input(dialog, /Bio/), 'Sees adults only.')

    expect(bodyOf(await saved(user, dialog))).toEqual({
      specialization: 'Interventional Cardiology',
      license_number: 'MCI-99001',
      bio: 'Sees adults only.',
    })
    await waitFor(() => expect(shown('Specialization')).toHaveTextContent('Interventional Cardiology'))
    expect(shown('Licence')).toHaveTextContent('MCI-99001')
    expect(screen.getByText('Sees adults only.')).toBeInTheDocument()
  })

  it('reopens with the saved values', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    let dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await saved(user, dialog)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    dialog = await openAction(user, 'Edit')
    expect(input(dialog, /Consultation fee/)).toHaveValue('1200.00')
    expect(saveButton(dialog)).toBeDisabled()
  })

  it('sends nothing when the edit comes to no change', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    // Different text, the same values: an empty body would be a 422.
    await user.type(input(dialog, /Specialization/), '  ')
    await retype(user, input(dialog, /Consultation fee/), '950')
    await user.click(saveButton(dialog))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(writes()).toHaveLength(0)
    expect(toastSuccess).toHaveBeenCalledWith('Nothing to save — the profile is unchanged')
  })

  it('saves a bio whose only change is the padding taken off it', async () => {
    // Written by another client: the API stores a bio as sent, blank lines and all.
    doctor = makeDoctor({ bio: '\n\nText with padding.  ' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    expect(input(dialog, /Bio/)).toHaveValue('\n\nText with padding.  ')
    await retype(user, input(dialog, /Bio/), 'Text with padding.')

    expect(bodyOf(await saved(user, dialog))).toEqual({ bio: 'Text with padding.' })
    expect(doctor.bio).toBe('Text with padding.')
  })

  it('saves a qualification whose only change is the padding taken off it', async () => {
    doctor = makeDoctor({ qualifications: [{ degree: ' MD ', institution: 'AIIMS Delhi' }] })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, within(qualification(dialog, 1)).getByLabelText(/Degree/), 'MD')

    expect(bodyOf(await saved(user, dialog))).toEqual({
      qualifications: [{ degree: 'MD', institution: 'AIIMS Delhi' }],
    })
  })

  it('does not send padded text that was left alone', async () => {
    doctor = makeDoctor({ bio: '\n\nText with padding.  ', qualifications: [{ degree: ' MD ' }] })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')

    // Cleaning them up was not asked for, and would undo someone else's edit of either.
    expect(bodyOf(await saved(user, dialog))).toEqual({ consultation_fee: '1200.00' })
    expect(doctor.bio).toBe('\n\nText with padding.  ')
  })

  it('leaves out a field someone else changed while the dialog was open', async () => {
    const user = userEvent.setup()
    const client = renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    // Another admin raises the fee, and the page refetches behind the dialog.
    doctor.consultation_fee = '1000.00'
    await act(() => client.invalidateQueries())
    await waitFor(() => expect(shown('Consultation fee')).toHaveTextContent(formatMoney('1000.00')))

    await retype(user, input(dialog, /Bio/), 'Sees adults only.')
    // The fee this form was opened with is not sent back over theirs.
    expect(bodyOf(await saved(user, dialog))).toEqual({ bio: 'Sees adults only.' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('1000.00'))
  })

  it('keeps an inactive current department selected, and does not resend it', async () => {
    departments.push({ id: 'dep-onco', code: 'ONCO', name: 'Oncology', location: null, status: 'inactive' })
    doctor = makeDoctor({ department_id: 'dep-onco', department_name: 'Oncology' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await waitFor(() => expect(department(dialog)).toHaveTextContent('Oncology (inactive)'))
    await departmentOptions(user, dialog, ['Unassigned', 'Oncology (inactive)', 'Cardiology', 'Neurology'])

    await retype(user, input(dialog, /Consultation fee/), '800')
    // Sending the department again would fail the save: it is no longer active.
    expect(bodyOf(await saved(user, dialog))).toEqual({ consultation_fee: '800.00' })
    await waitFor(() => expect(shown('Department')).toHaveTextContent('Oncology'))
  })

  it('sends one request while a save is in flight', async () => {
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(write(config))))
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await within(dialog).findByRole('button', { name: /Saving/ })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(writes()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(writes()).toHaveLength(1)
  })
})

describe('edit validation, before any request', () => {
  /** Replace what a field holds with `value`, try to save, and expect `message` under it instead. */
  async function expectRejected(field: (dialog: HTMLElement) => HTMLElement, value: string, message: string) {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    const target = field(dialog)
    await user.clear(target)
    // Pasted: the longest value here would take thousands of keystrokes.
    if (value) {
      await user.click(target)
      await user.paste(value)
    }
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText(message)).toBeInTheDocument()
    expect(target).toHaveAttribute('aria-invalid', 'true')
    expect(target).toHaveAccessibleDescription(message)
    expect(writes()).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  }

  it.each([
    [/Specialization/, '', 'Enter a specialization'],
    [/Specialization/, '   ', 'Enter a specialization'],
    [/Specialization/, 'x'.repeat(101), 'Use at most 100 characters'],
    [/Licence number/, '', 'Enter the licence number'],
    [/Licence number/, '   ', 'Enter the licence number'],
    [/Licence number/, 'x'.repeat(51), 'Use at most 50 characters'],
    [/Consultation fee/, '', 'Enter a fee, with at most 2 decimals'],
    [/Consultation fee/, '10.123', 'Enter a fee, with at most 2 decimals'],
    [/Consultation fee/, '-50', 'Enter a fee, with at most 2 decimals'],
    [/Consultation fee/, 'abc', 'Enter a fee, with at most 2 decimals'],
    [/Consultation fee/, '1000000', 'The fee cannot be more than 999999.99'],
    [/Consultation fee/, '1000000.00', 'The fee cannot be more than 999999.99'],
  ])('rejects %s "%s"', async (label, value, message) => {
    await expectRejected((dialog) => input(dialog, label), value, message)
  })

  // Nothing to see in the input, and nothing the API would keep: a NUL is
  // taken out before sending, and the API's own trim takes U+001F and U+0085
  // off with the spaces. Sent, each would come back as a 422 in its wording.
  it.each([
    [/Specialization/, 'a NUL', '\u0000', 'Enter a specialization'],
    [/Specialization/, 'U+001F between spaces', ' \u001f ', 'Enter a specialization'],
    [/Licence number/, 'NULs and a space', '\u0000 \u0000', 'Enter the licence number'],
    [/Licence number/, 'U+0085', '\u0085', 'Enter the licence number'],
  ])('rejects %s holding only %s', async (label, _what, value, message) => {
    await expectRejected((dialog) => input(dialog, label), value, message)
  })

  it('rejects a bio over 5000 characters', async () => {
    await expectRejected((dialog) => input(dialog, /Bio/), 'x'.repeat(5001), 'Use at most 5000 characters')
  })

  it.each([
    ['a degree of only a NUL', /Degree/, '\u0000', 'Enter the degree'],
    ['a degree over 100 characters', /Degree/, 'x'.repeat(101), 'Use at most 100 characters'],
    ['an institution over 200 characters', /Institution/, 'x'.repeat(201), 'Use at most 200 characters'],
  ])('rejects a qualification with %s', async (_what, label, value, message) => {
    await expectRejected((dialog) => within(qualification(dialog, 1)).getByLabelText(label), value, message)
  })

  it('accepts the largest fee the API takes', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '999999.99')

    expect(bodyOf(await saved(user, dialog))).toEqual({ consultation_fee: '999999.99' })
  })

  it.each(['1899', '2101', '20x8', '08'])('rejects the year "%s"', async (year) => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    const field = within(qualification(dialog, 1)).getByLabelText(/Year/)
    await retype(user, field, year)
    await user.click(saveButton(dialog))

    expect(
      await within(qualification(dialog, 1)).findByText('Enter a year between 1900 and 2100'),
    ).toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })

  it('requires a degree on every qualification', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.click(within(dialog).getByRole('button', { name: 'Add qualification' }))
    await user.type(within(qualification(dialog, 3)).getByLabelText(/Institution/), 'AIIMS Delhi')
    await user.click(saveButton(dialog))

    expect(await within(qualification(dialog, 3)).findByText('Enter the degree')).toBeInTheDocument()
    expect(within(qualification(dialog, 1)).queryByText('Enter the degree')).not.toBeInTheDocument()
    expect(writes()).toHaveLength(0)
  })
})

describe('an edit the API refuses', () => {
  it('shows a 422 on a qualification under that row\'s input', async () => {
    onWrite = () =>
      invalid([{ field: 'qualifications.0.degree', message: 'String should have at most 100 characters' }])
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, within(qualification(dialog, 1)).getByLabelText(/Degree/), 'MBBS (Hons)')
    await user.click(saveButton(dialog))

    const first = within(qualification(dialog, 1))
    expect(await first.findByText('String should have at most 100 characters')).toBeInTheDocument()
    expect(first.getByLabelText(/Degree/)).toHaveAccessibleDescription(
      'String should have at most 100 characters',
    )
    expect(within(qualification(dialog, 2)).getByLabelText(/Degree/)).toHaveAttribute('aria-invalid', 'false')
    // Still open, to be corrected; nothing was saved.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('950.00'))
  })

  it('shows a refused department under the department select', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    // Deactivated after this form loaded its list. The API answers in its
    // second 422 shape: the field errors one level down, at `errors.errors`.
    departments[1].status = 'inactive'
    await chooseDepartment(user, dialog, 'Neurology')
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText('Unknown department.')).toBeInTheDocument()
    expect(bodyOf(writes()[0])).toEqual({ department_id: 'dep-neuro' })
    expect(department(dialog)).toHaveAttribute('aria-invalid', 'true')
    expect(department(dialog)).toHaveAccessibleDescription('Unknown department.')
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(shown('Department')).toHaveTextContent('Cardiology')
  })

  it('shows a rule that names no field above the form, in plain words', async () => {
    onWrite = () =>
      invalid([{ field: '', message: 'Value error, These fields cannot be set to null: languages.' }])
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await user.clear(input(dialog, /Languages/))
    await user.click(saveButton(dialog))

    expect(
      await within(dialog).findByText('These fields cannot be set to null: languages.'),
    ).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('shows a permission refusal (403) in the dialog and leaves the record alone', async () => {
    onWrite = () =>
      fail(403, 'Permission denied. Required: doctor.update.', { error_code: 'PERMISSION_DENIED', errors: null })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText('Permission denied. Required: doctor.update.')).toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('950.00'))
  })

  it('shows a 404 in the dialog, then follows the record to its real state', async () => {
    // Deactivated by someone else after this page loaded: an edit is now a 404.
    onWrite = (config) => {
      doctor.status = 'inactive'
      return write(config)
    }
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    let release: () => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (release = () => resolve(ok(doctor))))
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText('Doctor not found.')).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()

    // The page asks again and finds the doctor deactivated, so editing is no
    // longer offered and the dialog goes with it. The reason stays on screen.
    await waitFor(() => expect(doctorReads()).toHaveLength(2))
    release()
    expect(await screen.findByText('This doctor is inactive')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(toastError).toHaveBeenCalledWith('Doctor not found.')
    expect(await actions()).toEqual(['Reactivate'])
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('950.00'))
  })

  it('does not show internal error text for a server failure, and can be retried', async () => {
    onWrite = () => fail(500, 'Traceback (most recent call last): asyncpg…', { error_code: 'INTERNAL_ERROR' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText("Couldn't save the changes. Please try again.")).toBeInTheDocument()
    expect(within(dialog).queryByText(/Traceback/)).not.toBeInTheDocument()

    onWrite = null
    await user.click(saveButton(dialog))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor updated'))
    expect(writes()).toHaveLength(2)
    expect(bodyOf(writes()[1])).toEqual({ consultation_fee: '1200.00' })
  })
})

describe('cancelling an edit', () => {
  it('sends nothing, leaves the record as it was, and reopens with the original values', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    let dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await retype(user, input(dialog, /Specialization/), 'Neurology')
    await chooseDepartment(user, dialog, 'Unassigned')
    await user.click(within(dialog).getByRole('button', { name: 'Remove qualification 1' }))
    await user.clear(input(dialog, /Bio/))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(writes()).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('950.00'))
    expect(shown('Department')).toHaveTextContent('Cardiology')
    expect(screen.getByText('MBBS (AIIMS Delhi, 2008) · MD')).toBeInTheDocument()

    dialog = await openAction(user, 'Edit')
    expect(input(dialog, /Consultation fee/)).toHaveValue('950.00')
    expect(input(dialog, /Specialization/)).toHaveValue('Cardiology')
    expect(department(dialog)).toHaveTextContent('Cardiology')
    expect(within(dialog).getAllByRole('listitem')).toHaveLength(2)
    expect(within(qualification(dialog, 1)).getByLabelText(/Degree/)).toHaveValue('MBBS')
    expect(input(dialog, /Bio/)).toHaveValue('Interventional cardiologist.')
    expect(saveButton(dialog)).toBeDisabled()
  })

  it('clears a validation error when the form is reopened', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    let dialog = await openAction(user, 'Edit')
    await user.clear(input(dialog, /Specialization/))
    await user.click(saveButton(dialog))
    expect(await within(dialog).findByText('Enter a specialization')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    dialog = await openAction(user, 'Edit')
    expect(within(dialog).queryByText('Enter a specialization')).not.toBeInTheDocument()
    expect(input(dialog, /Specialization/)).toHaveValue('Cardiology')
    expect(writes()).toHaveLength(0)
  })
})

describe('deactivating a doctor', () => {
  it('asks first, sends DELETE with no body, and shows the doctor as inactive', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    expect(within(dialog).getByText('Deactivate Priya Sharma?')).toBeInTheDocument()
    expect(within(dialog).getByText(/can no longer be booked for\s+appointments/)).toBeInTheDocument()
    expect(within(dialog).getByText(/can be\s+reversed by reactivating the doctor/)).toBeInTheDocument()
    expect(writes()).toHaveLength(0)

    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor deactivated'))
    expect(writes()).toHaveLength(1)
    const [request] = writes()
    expect(request.method).toBe('delete')
    expect(urlOf(request)).toBe('/api/v1/doctors/doc-1')
    expect(request.data).toBeUndefined()

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByText('This doctor is inactive')).toBeInTheDocument()
    expect(screen.getByText('inactive', { selector: 'span' })).toBeInTheDocument()
    expect(await actions()).toEqual(['Reactivate'])
    // Shown from the DELETE response — a plain read of this doctor is now a 404.
    expect(doctorReads()).toHaveLength(1)
  })

  it('does nothing when the confirmation is dismissed', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    await openAction(user, 'Deactivate')
    await user.click(screen.getByRole('button', { name: 'Keep active' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(writes()).toHaveLength(0)
    expect(await actions()).toEqual(['Edit', 'Deactivate'])
    expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument()
  })

  it('shows the API\'s reason when future appointments block it (409)', async () => {
    const reason =
      'Doctor cannot be deactivated while future appointments exist. Cancel or reassign them first.'
    onWrite = () =>
      fail(409, reason, {
        error_code: 'RESOURCE_CONFLICT',
        errors: { doctor_id: 'doc-1', future_appointments: 3 },
      })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))

    expect(await within(dialog).findByText(reason)).toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    // The page was refreshed, and the doctor is still active.
    await waitFor(() => expect(doctorReads()).toHaveLength(2))
    expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('catches up when someone else already deactivated the doctor (400)', async () => {
    onWrite = (config) => {
      doctor.status = 'inactive'
      return write(config)
    }
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Doctor is already deactivated.'))
    expect(await screen.findByText('This doctor is inactive')).toBeInTheDocument()
    expect(await actions()).toEqual(['Reactivate'])
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('shows a permission refusal (403) and leaves the doctor active', async () => {
    onWrite = () =>
      fail(403, 'Permission denied. Required: doctor.delete.', { error_code: 'PERMISSION_DENIED', errors: null })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))

    expect(await within(dialog).findByText('Permission denied. Required: doctor.delete.')).toBeInTheDocument()
    expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument()
  })

  it('sends one request while the deactivation is in flight', async () => {
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(write(config))))
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    expect(await within(dialog).findByRole('button', { name: /Deactivating/ })).toBeDisabled()
    expect(writes()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(writes()).toHaveLength(1)
  })
})

describe('reactivating a doctor', () => {
  beforeEach(() => {
    doctor = makeDoctor({ status: 'inactive' })
  })

  it('posts to /activate with no body and the doctor is active again', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    await pageLoaded()
    expect(screen.getByText('This doctor is inactive')).toBeInTheDocument()
    await user.click(action('Reactivate'))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor reactivated'))
    expect(writes()).toHaveLength(1)
    const [request] = writes()
    expect(request.method).toBe('post')
    expect(urlOf(request)).toBe('/api/v1/doctors/doc-1/activate')
    expect(request.data).toBeUndefined()

    await waitFor(() => expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument())
    expect(screen.getByText('active', { selector: 'span' })).toBeInTheDocument()
    expect(await actions()).toEqual(['Edit', 'Deactivate'])
    expect(doctorReads()).toHaveLength(1)
  })

  it('can be edited again once reactivated', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    await pageLoaded()
    await user.click(action('Reactivate'))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor reactivated'))

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await user.click(saveButton(dialog))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor updated'))
    expect(writes().map((c) => c.method)).toEqual(['post', 'patch'])
  })

  it('catches up when the doctor is already active (400)', async () => {
    onWrite = (config) => {
      doctor.status = 'active'
      return write(config)
    }
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    await pageLoaded()
    await user.click(action('Reactivate'))

    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Doctor is already active.'))
    await waitFor(() => expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument())
    expect(await actions()).toEqual(['Edit', 'Deactivate'])
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('reports a server failure without internal text, and stays inactive', async () => {
    onWrite = () => fail(500, 'Traceback (most recent call last): asyncpg…', { error_code: 'INTERNAL_ERROR' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    await pageLoaded()
    await user.click(action('Reactivate'))

    await waitFor(() =>
      expect(toastError).toHaveBeenCalledWith("Couldn't reactivate the doctor. Please try again."),
    )
    expect(screen.getByText('This doctor is inactive')).toBeInTheDocument()
    expect(action('Reactivate')).toBeEnabled()
  })

  it('sends one request while the reactivation is in flight', async () => {
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(write(config))))
    renderDoctor(ADMIN)

    await pageLoaded()
    const button = action('Reactivate')
    // Two clicks in the same tick, before React can disable the button.
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => expect(button).toHaveTextContent('Reactivating…'))
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('aria-busy', 'true')
    expect(writes()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(writes()).toHaveLength(1)
  })
})

describe('a read that lands after a write', () => {
  /**
   * Refetch the doctor, with the server answering at once — from the record as
   * it is now — and the answer held on the way back. Returns the function
   * that lets it arrive: a slow response, overtaken by whatever is done first.
   */
  async function overtakenRead(client: QueryClient) {
    const answer = ok(structuredClone(doctor))
    let arrive: () => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (arrive = () => resolve(answer)))
    const before = doctorReads().length
    act(() => {
      void client.invalidateQueries({ queryKey: doctorKeys.detail('doc-1') })
    })
    await waitFor(() => expect(doctorReads()).toHaveLength(before + 1))
    onRead = null
    return async () => {
      arrive()
      await settle()
    }
  }

  it('does not put the old record back over a saved edit', async () => {
    const user = userEvent.setup()
    const client = renderDoctor(ADMIN)
    await pageLoaded()
    const arrive = await overtakenRead(client)

    let dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await saved(user, dialog)
    await waitFor(() => expect(shown('Consultation fee')).toHaveTextContent(formatMoney('1200.00')))
    await arrive()

    expect(shown('Consultation fee')).toHaveTextContent(formatMoney('1200.00'))
    dialog = await openAction(user, 'Edit')
    expect(input(dialog, /Consultation fee/)).toHaveValue('1200.00')
  })

  it('does not show a reactivated doctor as inactive again', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    const user = userEvent.setup()
    const client = renderDoctor(ADMIN)
    await pageLoaded()
    const arrive = await overtakenRead(client)

    await user.click(action('Reactivate'))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor reactivated'))
    await arrive()

    expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument()
    expect(await actions()).toEqual(['Edit', 'Deactivate'])
  })

  it('does not show a deactivated doctor as active again', async () => {
    const user = userEvent.setup()
    const client = renderDoctor(ADMIN)
    await pageLoaded()
    const arrive = await overtakenRead(client)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor deactivated'))
    await arrive()

    expect(screen.getByText('This doctor is inactive')).toBeInTheDocument()
    expect(await actions()).toEqual(['Reactivate'])
  })
})

describe('the directory after a write', () => {
  const directoryRow = () => screen.getByRole('row', { name: /Priya Sharma/ })

  async function openFromDirectory(user: User) {
    await user.click(await screen.findByRole('link', { name: 'View Priya Sharma' }))
    await pageLoaded()
  }

  async function backToDirectory(user: User) {
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('link', { name: /Back to doctors/ }))
  }

  // Each return is seconds after the directory was fetched, so what it holds
  // is still fresh: it is only asked for again because the write said to.

  it('shows an edited fee', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN, '/doctors')
    await waitFor(() => expect(directoryRow()).toHaveTextContent(formatMoney('950.00')))
    await openFromDirectory(user)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await saved(user, dialog)
    await backToDirectory(user)

    await waitFor(() => expect(directoryRow()).toHaveTextContent(formatMoney('1200.00')))
    expect(listReads()).toHaveLength(2)
  })

  it('no longer lists a deactivated doctor', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN, '/doctors')
    await openFromDirectory(user)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor deactivated'))
    await backToDirectory(user)

    expect(await screen.findByText('No doctors yet')).toBeInTheDocument()
    expect(screen.queryByText('Priya Sharma')).not.toBeInTheDocument()
    expect(listReads()).toHaveLength(2)
  })

  it('lists a reactivated doctor again', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    const user = userEvent.setup()
    renderDoctor(ADMIN, '/doctors')
    expect(await screen.findByText('No doctors yet')).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Include inactive' }))
    await openFromDirectory(user)

    await user.click(action('Reactivate'))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Doctor reactivated'))
    await backToDirectory(user)

    // Back on active doctors only: the list fetched first, when it was empty.
    await waitFor(() => expect(within(directoryRow()).getByText('active')).toBeInTheDocument())
    expect(listReads().map((c) => c.params.include_inactive)).toEqual([undefined, true, undefined])
  })
})

describe('keyboard focus when a change of status replaces the actions', () => {
  const group = () => screen.getByRole('group', { name: 'Doctor actions' })

  it('stays with the actions after reactivating', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    const user = userEvent.setup()
    renderDoctor(ADMIN)
    await pageLoaded()

    action('Reactivate').focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument())
    await settle()

    // The button that held it is gone. Tab goes on to what can be done now,
    // not back to the top of the page.
    expect(group()).toHaveFocus()
    await user.tab()
    expect(action('Edit')).toHaveFocus()
  })

  it('stays with the actions after deactivating', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Deactivate')
    await user.click(within(dialog).getByRole('button', { name: 'Deactivate doctor' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(await screen.findByText('This doctor is inactive')).toBeInTheDocument()
    await settle()

    // The dialog and the button it would have returned to went together.
    expect(group()).toHaveFocus()
    await user.tab()
    expect(action('Reactivate')).toHaveFocus()
  })

  it('stays with the actions when the doctor turns out to be active already (400)', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    onWrite = (config) => {
      doctor.status = 'active'
      return write(config)
    }
    const user = userEvent.setup()
    renderDoctor(ADMIN)
    await pageLoaded()

    action('Reactivate').focus()
    await user.keyboard('{Enter}')
    await waitFor(() => expect(toastError).toHaveBeenCalledWith('Doctor is already active.'))
    await waitFor(() => expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument())
    await settle()

    expect(group()).toHaveFocus()
  })

  it('is left alone where the user has moved it in the meantime', async () => {
    doctor = makeDoctor({ status: 'inactive' })
    let finish: () => void = () => {}
    onWrite = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(write(config))))
    const user = userEvent.setup()
    renderDoctor(ADMIN)
    await pageLoaded()

    await user.click(action('Reactivate'))
    await waitFor(() => expect(writes()).toHaveLength(1))
    const back = screen.getByRole('link', { name: /Back to doctors/ })
    back.focus()
    finish()
    await waitFor(() => expect(screen.queryByText('This doctor is inactive')).not.toBeInTheDocument())
    await settle()

    expect(back).toHaveFocus()
  })

  it('returns to Edit after a save, which replaces nothing', async () => {
    const user = userEvent.setup()
    renderDoctor(ADMIN)

    const dialog = await openAction(user, 'Edit')
    await retype(user, input(dialog, /Consultation fee/), '1200')
    await saved(user, dialog)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await settle()

    expect(action('Edit')).toHaveFocus()
  })
})
