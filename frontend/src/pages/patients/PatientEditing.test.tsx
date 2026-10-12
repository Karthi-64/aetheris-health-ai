import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { InternalAxiosRequestConfig } from 'axios'
import { appointmentKeys } from '@/api/appointments'
import { billingKeys } from '@/api/billing'
import { patientKeys, type Patient } from '@/api/patients'
import { bodyOf, fail, installFakeApi, ok, paged, type FakeApi, type Outcome } from '@/test/fakeApi'
import { signIn, signOut } from '@/test/auth'
import PatientDetailPage from './PatientDetailPage'
import PatientsPage from './PatientsPage'

/**
 * Editing a patient from the detail page, against `PATCH /patients/{id}`
 * (`UpdatePatientRequest` in backend/app/schemas/patient.py).
 *
 * The real hooks, permission check, `http` wrapper and Axios instance run
 * against an in-memory server. It stores a PATCH the way the API does — each
 * top-level key sent replaces what was stored, whole — and serves the result
 * on the next read. It does not normalize: a test that depends on the API
 * rewriting a value states what ends up stored in `stored`.
 */

const { toastSuccess, toastError } = vi.hoisted(() => ({
  toastSuccess: vi.fn(),
  toastError: vi.fn(),
}))
vi.mock('sonner', () => ({ toast: { success: toastSuccess, error: toastError } }))

const ID = '3f6c1b2e-0000-4000-8000-000000000001'

// Role → patient permissions as seeded in backend/app/seeds/seed.py.
const ADMIN = ['patient.read', 'patient.create', 'patient.update', 'patient.delete']
const DOCTOR = ['patient.read', 'patient.create', 'patient.update']
const NURSE = ['patient.read', 'patient.update']
const RECEPTIONIST = ['patient.read', 'patient.create']
const BILLING_STAFF = ['patient.read']

function thomas(overrides: Partial<Patient> = {}): Patient {
  return {
    id: ID,
    hospital_id: 'hosp-1',
    mrn: 'MRN-2026-00042',
    first_name: 'Thomas',
    last_name: 'George',
    full_name: 'Thomas George',
    date_of_birth: '1971-05-02',
    age: 55,
    gender: 'male',
    blood_group: 'O+',
    phone: '+919812345678',
    email: 'thomas.george@example.com',
    // Nested values hold only the keys that were sent — there is no `line2` here.
    address: {
      line1: '14 Marine Drive',
      city: 'Kochi',
      state: 'Kerala',
      postal_code: '682031',
      country: 'IN',
    },
    emergency_contact: { name: 'Meera George', phone: '+919812300000', relation: 'Spouse' },
    marital_status: 'Married',
    occupation: 'Teacher',
    allergies: [
      { name: 'Penicillin', severity: 'severe', reaction: 'Hives', noted_on: '2019-04-11' },
      // Saved by an update that left the severity out, so none was stored.
      { name: 'Latex' },
    ],
    // Registered through POST, which stores an unset optional as null.
    chronic_conditions: [{ name: 'Type 2 Diabetes', since_year: 2015, notes: null }],
    current_medications: [
      { name: 'Metformin', dosage: '500mg', frequency: 'Twice daily', started_on: '2015-06-01' },
    ],
    notes: 'Prefers morning appointments.',
    status: 'active',
    created_at: '2026-07-27T09:00:00Z',
    updated_at: '2026-09-30T11:20:00Z',
    ...overrides,
  }
}

/** A record with nothing optional on it. */
const BARE: Partial<Patient> = {
  blood_group: null,
  phone: null,
  email: null,
  address: null,
  emergency_contact: null,
  marital_status: null,
  occupation: null,
  allergies: [],
  chronic_conditions: [],
  current_medications: [],
  notes: null,
}

const notFound = () =>
  fail(404, 'Patient not found.', { error_code: 'RESOURCE_NOT_FOUND', errors: { patient_id: ID } })

const invalid = (errors: unknown, message = 'Validation failed.') =>
  fail(422, message, { error_code: 'VALIDATION_ERROR', errors })

let api: FakeApi
let client: QueryClient
/** The record the server holds. */
let patient: Patient
/** What the server stores differently from what it was sent (a normalized phone, a lowercased email). */
let stored: Partial<Patient>
/** Override to refuse, or delay, the next update. */
let onPatch: ((config: InternalAxiosRequestConfig) => Outcome | Promise<Outcome>) | null
/** Override the patient read, e.g. to fail or delay it. */
let onRead: (() => Outcome | Promise<Outcome>) | null
/** The rows the registry lists, when it is not just `patient`. */
let registry: Patient[] | null

function applyPatch(config: InternalAxiosRequestConfig): Outcome {
  Object.assign(patient, bodyOf(config), stored)
  patient.full_name = `${patient.first_name} ${patient.last_name}`
  return ok(structuredClone(patient))
}

beforeEach(() => {
  toastSuccess.mockReset()
  toastError.mockReset()
  patient = thomas()
  stored = {}
  onPatch = null
  onRead = null
  registry = null
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  api = installFakeApi((config) => {
    const url = config.url ?? ''
    if (config.method === 'patch' && url === `/patients/${ID}`) return (onPatch ?? applyPatch)(config)
    if (config.method !== 'get') return fail(405, 'Method Not Allowed', { error_code: 'INTERNAL_ERROR' })
    if (url === `/patients/${ID}`) return onRead ? onRead() : ok(structuredClone(patient))
    if (url === '/patients') return paged(structuredClone(registry ?? [patient]))
    if (url === '/invoices') return paged([])
    return fail(404, 'Not Found', { error_code: 'RESOURCE_NOT_FOUND' })
  })
})

afterEach(() => {
  api.restore()
  signOut()
})

type User = ReturnType<typeof userEvent.setup>

function renderPatient(permissions: string[]) {
  signIn(permissions)
  render(
    <MemoryRouter initialEntries={[`/patients/${ID}`]}>
      <QueryClientProvider client={client}>
        <Routes>
          <Route path="/patients/:patientId" element={<PatientDetailPage />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

const loaded = () => screen.findByRole('heading', { name: patient.full_name })
const editButton = () => screen.queryByRole('button', { name: 'Edit patient' })
const patches = () => api.requests('patch')
const reads = () => api.requests('get', `/patients/${ID}`)

async function openEdit(user: User) {
  await user.click(await screen.findByRole('button', { name: 'Edit patient' }))
  return screen.findByRole('dialog')
}

/** The fields under one heading of the form ("Address"), or one history list ("Allergies"). */
const group = (dialog: HTMLElement, name: string) =>
  within(within(dialog).getByRole('group', { name }))

/** One row of a history list, by its name ("Allergy 2"). */
const row = (dialog: HTMLElement, name: string) =>
  within(within(dialog).getByRole('listitem', { name }))

const saveButton = (dialog: HTMLElement) => within(dialog).getByRole('button', { name: 'Save changes' })

async function retype(user: User, input: HTMLElement, value: string) {
  await user.clear(input)
  if (value) await user.type(input, value)
}

/** Replace a field's content in one step — for values too long to type a key at a time. */
async function fill(user: User, input: HTMLElement, value: string) {
  await user.clear(input)
  await user.click(input)
  await user.paste(value)
}

/** A field of the form, by the heading it sits under and its label. */
const field = (heading: string, label: string | RegExp) => (dialog: HTMLElement) =>
  group(dialog, heading).getByLabelText(label)

/** The name field of a history row ("Allergy 1"), or another field of that row by its label. */
const rowField = (name: string, label?: string) => (dialog: HTMLElement) =>
  label
    ? row(dialog, name).getByLabelText(label)
    : row(dialog, name).getByRole('textbox', { name: new RegExp(name) })

/** A background refresh of the record, as a reconnect or a revisit would trigger. */
const refresh = () => act(() => client.invalidateQueries({ queryKey: patientKeys.detail(ID) }))

const unavailable = () => fail(503, 'Service Unavailable', { error_code: 'INTERNAL_ERROR' })

const PHONE_FORMAT = 'Enter a 10-digit mobile number, or include the country code, e.g. +91 98123 45678'

async function choose(user: User, select: HTMLElement, option: string) {
  await user.click(select)
  await user.click(await screen.findByRole('option', { name: option }))
}

/** Save, wait for it to go through, and return the one PATCH that was sent. */
async function save(user: User, dialog: HTMLElement) {
  await user.click(saveButton(dialog))
  await waitFor(() => expect(toastSuccess).toHaveBeenCalled())
  expect(patches()).toHaveLength(1)
  return patches()[0]
}

/** The value the detail page shows beside a label, in the card with this title. */
function shown(card: string, label: string) {
  const section = screen.getByRole('heading', { name: card }).closest('section') as HTMLElement
  return within(section).getByText(label).nextElementSibling
}

describe('patient detail', () => {
  it('shows a loading state, then the record', async () => {
    let finish: (outcome: Outcome) => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (finish = resolve))
    renderPatient(ADMIN)

    expect(await screen.findByRole('status', { name: 'Loading patient' })).toBeInTheDocument()
    expect(editButton()).not.toBeInTheDocument()

    finish(ok(patient))
    expect(await loaded()).toBeInTheDocument()
    expect(screen.getByText('MRN-2026-00042')).toBeInTheDocument()
  })

  it('shows every field the edit form can change', async () => {
    renderPatient(BILLING_STAFF)
    await loaded()

    expect(shown('Demographics', 'Gender')).toHaveTextContent('Male')
    expect(shown('Demographics', 'Blood group')).toHaveTextContent('O+')
    expect(shown('Demographics', 'Marital status')).toHaveTextContent('Married')
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Teacher')
    expect(shown('Contact', 'Phone')).toHaveTextContent('+919812345678')
    expect(shown('Contact', 'Email')).toHaveTextContent('thomas.george@example.com')
    expect(shown('Contact', 'Address')).toHaveTextContent('14 Marine Drive, Kochi, Kerala, 682031, IN')
    expect(shown('Emergency contact', 'Name')).toHaveTextContent('Meera George')
    expect(shown('Emergency contact', 'Relationship')).toHaveTextContent('Spouse')
    expect(shown('Emergency contact', 'Phone')).toHaveTextContent('+919812300000')
    // Severity sits beside the allergy it belongs to, where one was recorded.
    expect(shown('Medical history', 'Allergies')).toHaveTextContent('Penicillin (severe), Latex')
    expect(shown('Medical history', 'Chronic conditions')).toHaveTextContent('Type 2 Diabetes')
    expect(shown('Medical history', 'Current medications')).toHaveTextContent('Metformin')
    expect(shown('Notes', 'Administrative notes')).toHaveTextContent('Prefers morning appointments.')
  })

  it('shows a dash for what is not on record', async () => {
    patient = thomas(BARE)
    renderPatient(BILLING_STAFF)
    await loaded()

    expect(shown('Emergency contact', 'Name')).toHaveTextContent('—')
    expect(shown('Notes', 'Administrative notes')).toHaveTextContent('—')
  })

  it('says so when the record cannot be loaded', async () => {
    onRead = notFound
    renderPatient(ADMIN)

    expect(await screen.findByText("Couldn't load this patient")).toBeInTheDocument()
    expect(editButton()).not.toBeInTheDocument()
  })

  it('keeps the record on screen when a refresh fails, and says it may be out of date', async () => {
    const user = userEvent.setup()
    renderPatient(BILLING_STAFF)
    await loaded()

    onRead = unavailable
    await refresh()

    expect(await screen.findByText("Couldn't refresh this record")).toBeInTheDocument()
    expect(screen.queryByText("Couldn't load this patient")).not.toBeInTheDocument()
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Teacher')

    // Once the server answers again, the notice goes and the page catches up.
    onRead = null
    patient.occupation = 'Headteacher'
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(screen.queryByText("Couldn't refresh this record")).not.toBeInTheDocument())
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Headteacher')
  })

  it.each([
    ['is gone', notFound],
    [
      'may no longer be read by this user',
      () => fail(403, 'Permission denied. Required: patient.read.', { error_code: 'PERMISSION_DENIED' }),
    ],
  ])('stops showing a record that, on a refresh, %s', async (_case, refusal) => {
    renderPatient(ADMIN)
    await loaded()

    onRead = refusal
    await refresh()

    expect(await screen.findByText("Couldn't load this patient")).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Thomas George' })).not.toBeInTheDocument()
    expect(editButton()).not.toBeInTheDocument()
  })
})

describe('who can edit a patient', () => {
  it.each([
    ['a hospital admin', ADMIN],
    ['a doctor', DOCTOR],
    ['a nurse', NURSE],
  ])('offers Edit to %s', async (_role, permissions) => {
    renderPatient(permissions)
    await loaded()
    expect(editButton()).toBeInTheDocument()
  })

  it.each([
    ['a receptionist', RECEPTIONIST],
    ['billing staff', BILLING_STAFF],
  ])('hides Edit from %s, who lacks patient.update', async (_role, permissions) => {
    renderPatient(permissions)
    await loaded()
    expect(editButton()).not.toBeInTheDocument()
  })

  it('hides Edit on an inactive patient, whose record the API will not update', async () => {
    patient = thomas({ status: 'inactive' })
    renderPatient(ADMIN)
    await loaded()

    expect(screen.getByText('inactive')).toBeInTheDocument()
    expect(editButton()).not.toBeInTheDocument()
  })
})

describe('opening the edit form', () => {
  it('shows the MRN as a fact, not a field', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    expect(within(dialog).getByText('MRN-2026-00042')).toBeInTheDocument()
    expect(within(dialog).getByText(/can't be changed/)).toBeInTheDocument()
    expect(within(dialog).queryByDisplayValue('MRN-2026-00042')).not.toBeInTheDocument()
  })

  it('starts from the record, including its address, contact and history rows', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    const identity = group(dialog, 'Identity')
    expect(identity.getByLabelText(/First name/)).toHaveValue('Thomas')
    expect(identity.getByLabelText(/Last name/)).toHaveValue('George')
    expect(identity.getByLabelText(/Date of birth/)).toHaveValue('1971-05-02')
    expect(identity.getByRole('combobox', { name: /Gender/ })).toHaveTextContent('Male')
    expect(identity.getByRole('combobox', { name: /Blood group/ })).toHaveTextContent('O+')
    expect(identity.getByLabelText('Marital status')).toHaveValue('Married')
    expect(identity.getByLabelText('Occupation')).toHaveValue('Teacher')

    const contact = group(dialog, 'Contact')
    expect(contact.getByLabelText('Phone')).toHaveValue('+919812345678')
    expect(contact.getByLabelText('Email')).toHaveValue('thomas.george@example.com')

    const address = group(dialog, 'Address')
    expect(address.getByLabelText('Line 1')).toHaveValue('14 Marine Drive')
    expect(address.getByLabelText('Line 2')).toHaveValue('')
    expect(address.getByLabelText('City')).toHaveValue('Kochi')
    expect(address.getByLabelText('State')).toHaveValue('Kerala')
    expect(address.getByLabelText('Postal code')).toHaveValue('682031')
    expect(address.getByLabelText('Country')).toHaveValue('IN')

    const emergency = group(dialog, 'Emergency contact')
    expect(emergency.getByLabelText('Name')).toHaveValue('Meera George')
    expect(emergency.getByLabelText('Phone')).toHaveValue('+919812300000')
    expect(emergency.getByLabelText('Relationship')).toHaveValue('Spouse')

    expect(group(dialog, 'Allergies').getAllByRole('listitem')).toHaveLength(2)
    const penicillin = row(dialog, 'Allergy 1')
    expect(penicillin.getByRole('textbox', { name: /Allergy 1/ })).toHaveValue('Penicillin')
    expect(penicillin.getByRole('combobox', { name: 'Severity' })).toHaveTextContent('Severe')
    expect(penicillin.getByLabelText('Reaction')).toHaveValue('Hives')
    expect(penicillin.getByLabelText('Noted on')).toHaveValue('2019-04-11')
    // Stored with a name only: the rest is blank, and the severity is the API's default.
    const latex = row(dialog, 'Allergy 2')
    expect(latex.getByRole('textbox', { name: /Allergy 2/ })).toHaveValue('Latex')
    expect(latex.getByRole('combobox', { name: 'Severity' })).toHaveTextContent('Moderate')
    expect(latex.getByLabelText('Reaction')).toHaveValue('')
    expect(latex.getByLabelText('Noted on')).toHaveValue('')

    const diabetes = row(dialog, 'Condition 1')
    expect(diabetes.getByRole('textbox', { name: /Condition 1/ })).toHaveValue('Type 2 Diabetes')
    expect(diabetes.getByLabelText('Since year')).toHaveValue('2015')
    expect(diabetes.getByLabelText('Notes')).toHaveValue('')

    const metformin = row(dialog, 'Medication 1')
    expect(metformin.getByRole('textbox', { name: /Medication 1/ })).toHaveValue('Metformin')
    expect(metformin.getByLabelText('Dosage')).toHaveValue('500mg')
    expect(metformin.getByLabelText('Frequency')).toHaveValue('Twice daily')
    expect(metformin.getByLabelText('Started on')).toHaveValue('2015-06-01')

    expect(group(dialog, 'Notes').getByLabelText('Administrative notes')).toHaveValue(
      'Prefers morning appointments.',
    )
    expect(patches()).toHaveLength(0)
  })

  it('starts blank where the record holds nothing', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    expect(group(dialog, 'Identity').getByRole('combobox', { name: /Blood group/ })).toHaveTextContent(
      'Not recorded',
    )
    expect(group(dialog, 'Contact').getByLabelText('Phone')).toHaveValue('')
    expect(group(dialog, 'Address').getByLabelText('Line 1')).toHaveValue('')
    expect(group(dialog, 'Emergency contact').getByLabelText('Name')).toHaveValue('')
    expect(within(dialog).getByText('No allergies recorded.')).toBeInTheDocument()
    expect(within(dialog).getByText('No chronic conditions recorded.')).toBeInTheDocument()
    expect(within(dialog).getByText('No current medications recorded.')).toBeInTheDocument()
    expect(within(dialog).queryByRole('listitem')).not.toBeInTheDocument()
  })

  it('keeps Save disabled until something is changed, and again once it is changed back', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const occupation = group(dialog, 'Identity').getByLabelText('Occupation')

    expect(saveButton(dialog)).toBeDisabled()
    await retype(user, occupation, 'Headteacher')
    expect(saveButton(dialog)).toBeEnabled()
    await retype(user, occupation, 'Teacher')
    expect(saveButton(dialog)).toBeDisabled()
    expect(patches()).toHaveLength(0)
  })
})

describe('saving a change', () => {
  it('sends a PATCH carrying only the field that changed, and shows the saved record', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    const request = await save(user, dialog)

    expect(request.method).toBe('patch')
    expect(`${request.baseURL}${request.url}`).toBe(`/api/v1/patients/${ID}`)
    expect(bodyOf(request)).toEqual({ occupation: 'Headteacher' })
    expect(toastSuccess).toHaveBeenCalledWith('Saved changes to Thomas George')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Headteacher')
    // The response is the saved record, so no second read was needed to show it.
    expect(reads()).toHaveLength(1)
  })

  it('trims a name, and the page takes the full name from the response', async () => {
    const user = userEvent.setup()
    renderPatient(DOCTOR)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText(/First name/), '  Tom ')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ first_name: 'Tom' })
    expect(await screen.findByRole('heading', { name: 'Tom George' })).toBeInTheDocument()
  })

  it('sends a date of birth and a gender in the API\'s own form', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const identity = group(dialog, 'Identity')

    await retype(user, identity.getByLabelText(/Date of birth/), '1971-05-20')
    await choose(user, identity.getByRole('combobox', { name: /Gender/ }), 'Not specified')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ date_of_birth: '1971-05-20', gender: 'unspecified' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Demographics', 'Gender')).toHaveTextContent('Not specified')
  })

  it('sends null for a cleared phone, never an empty string', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.clear(group(dialog, 'Contact').getByLabelText('Phone'))
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ phone: null })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Contact', 'Phone')).toHaveTextContent('—')
  })

  it('sends null when the blood group is set to "Not recorded"', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await choose(user, group(dialog, 'Identity').getByRole('combobox', { name: /Blood group/ }), 'Not recorded')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ blood_group: null })
  })

  it('sends a chosen blood group as its value', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await choose(user, group(dialog, 'Identity').getByRole('combobox', { name: /Blood group/ }), 'AB-')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ blood_group: 'AB-' })
  })

  it('sends null for emptied free text, which the API would otherwise store as ""', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.clear(group(dialog, 'Identity').getByLabelText('Marital status'))
    await user.clear(group(dialog, 'Identity').getByLabelText('Occupation'))
    await user.clear(group(dialog, 'Contact').getByLabelText('Email'))
    await user.clear(group(dialog, 'Notes').getByLabelText('Administrative notes'))
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      marital_status: null,
      occupation: null,
      email: null,
      notes: null,
    })
  })

  it('never sends an empty body: a change of spacing alone saves nothing', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.type(group(dialog, 'Identity').getByLabelText('Occupation'), '  ')
    expect(saveButton(dialog)).toBeEnabled()
    await user.click(saveButton(dialog))

    await waitFor(() =>
      expect(toastSuccess).toHaveBeenCalledWith('Nothing to save — the record is unchanged'),
    )
    expect(patches()).toHaveLength(0)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('sends one request while a save is in flight', async () => {
    let finish: () => void = () => {}
    onPatch = (config) => new Promise<Outcome>((resolve) => (finish = () => resolve(applyPatch(config))))
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    const form = dialog.querySelector('form') as HTMLFormElement
    fireEvent.submit(form)
    fireEvent.submit(form)

    const busy = await within(dialog).findByRole('button', { name: /Saving/ })
    expect(busy).toBeDisabled()
    expect(patches()).toHaveLength(1)

    finish()
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledTimes(1))
    expect(patches()).toHaveLength(1)
  })
})

describe('address', () => {
  it('resends the whole address when one line changes, because the API replaces it', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Address').getByLabelText('City'), 'Ernakulam')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      address: {
        line1: '14 Marine Drive',
        city: 'Ernakulam',
        state: 'Kerala',
        postal_code: '682031',
        country: 'IN',
      },
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Contact', 'Address')).toHaveTextContent('14 Marine Drive, Ernakulam, Kerala, 682031, IN')
  })

  it('sends null when every address field is emptied', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const address = group(dialog, 'Address')

    for (const label of ['Line 1', 'City', 'State', 'Postal code', 'Country']) {
      await user.clear(address.getByLabelText(label))
    }
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ address: null })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Contact', 'Address')).toHaveTextContent('—')
  })

  it('adds an address with the country uppercased and the blank optionals left out', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const address = group(dialog, 'Address')

    await user.type(address.getByLabelText('Line 1'), ' 7 Hill Road ')
    await user.type(address.getByLabelText('City'), 'Munnar')
    await user.type(address.getByLabelText('Country'), 'in')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      address: { line1: '7 Hill Road', city: 'Munnar', country: 'IN' },
    })
  })

  it('blocks a partly emptied address before any request', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const address = group(dialog, 'Address')

    await user.clear(address.getByLabelText('Line 1'))
    await user.click(saveButton(dialog))

    expect(await address.findByText('An address needs its first line')).toBeInTheDocument()
    expect(address.getByLabelText('Line 1')).toHaveAccessibleDescription('An address needs its first line')
    expect(patches()).toHaveLength(0)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('names each part a new address is missing', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const address = group(dialog, 'Address')

    await user.type(address.getByLabelText('State'), 'Kerala')
    await user.click(saveButton(dialog))

    expect(await address.findByText('An address needs its first line')).toBeInTheDocument()
    expect(address.getByText('An address needs a city')).toBeInTheDocument()
    expect(address.getByText('Enter the two-letter country code, e.g. IN')).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })

  it('rejects a country that is not a two-letter code', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const address = group(dialog, 'Address')

    await retype(user, address.getByLabelText('Country'), 'IND')
    await user.click(saveButton(dialog))

    expect(await address.findByText('Enter the two-letter country code, e.g. IN')).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })
})

describe('emergency contact', () => {
  it('resends the whole contact when one part changes', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Emergency contact').getByLabelText('Relationship'), 'Wife')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      emergency_contact: { name: 'Meera George', phone: '+919812300000', relation: 'Wife' },
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Emergency contact', 'Relationship')).toHaveTextContent('Wife')
  })

  it('sends null when every contact field is emptied', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const emergency = group(dialog, 'Emergency contact')

    for (const label of ['Name', 'Phone', 'Relationship']) {
      await user.clear(emergency.getByLabelText(label))
    }
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ emergency_contact: null })
  })

  it('blocks a contact that is missing a part before any request', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const emergency = group(dialog, 'Emergency contact')

    await user.type(emergency.getByLabelText('Name'), 'Meera George')
    await user.click(saveButton(dialog))

    expect(await emergency.findByText("Enter the contact's phone number")).toBeInTheDocument()
    expect(emergency.getByText('Say how the contact is related to the patient')).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })
})

describe('medical history', () => {
  it('resends the whole allergy list when a row is added, every item with a severity', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add allergy' }))
    await user.type(row(dialog, 'Allergy 3').getByRole('textbox', { name: /Allergy 3/ }), 'Peanuts')
    const request = await save(user, dialog)

    // Only the list that changed is sent. Blank optionals are left out, and
    // the row stored without a severity is sent with the default it showed.
    expect(bodyOf(request)).toEqual({
      allergies: [
        { name: 'Penicillin', severity: 'severe', reaction: 'Hives', noted_on: '2019-04-11' },
        { name: 'Latex', severity: 'moderate' },
        { name: 'Peanuts', severity: 'moderate' },
      ],
    })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Medical history', 'Allergies')).toHaveTextContent(
      'Penicillin (severe), Latex (moderate), Peanuts (moderate)',
    )
  })

  it('sends every part of a new allergy that was filled in', async () => {
    patient = thomas(BARE)
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add allergy' }))
    const allergy = row(dialog, 'Allergy 1')
    await user.type(allergy.getByRole('textbox', { name: /Allergy 1/ }), 'Shellfish')
    await choose(user, allergy.getByRole('combobox', { name: 'Severity' }), 'Mild')
    await user.type(allergy.getByLabelText('Reaction'), 'Rash')
    await user.type(allergy.getByLabelText('Noted on'), '2024-02-29')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      allergies: [{ name: 'Shellfish', severity: 'mild', reaction: 'Rash', noted_on: '2024-02-29' }],
    })
  })

  it('resends the list without a removed row, and renumbers the rest', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 1' }))
    expect(group(dialog, 'Allergies').getAllByRole('listitem')).toHaveLength(1)
    expect(row(dialog, 'Allergy 1').getByRole('textbox', { name: /Allergy 1/ })).toHaveValue('Latex')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ allergies: [{ name: 'Latex', severity: 'moderate' }] })
  })

  it('sends an empty list, not null, when the last row is removed', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Remove medication 1' }))
    expect(within(dialog).getByText('No current medications recorded.')).toBeInTheDocument()
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ current_medications: [] })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Medical history', 'Current medications')).toHaveTextContent('—')
  })

  it('sends a condition\'s onset year as a number', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add condition' }))
    const condition = row(dialog, 'Condition 2')
    await user.type(condition.getByRole('textbox', { name: /Condition 2/ }), 'Hypertension')
    await user.type(condition.getByLabelText('Since year'), '2020')
    await user.type(condition.getByLabelText('Notes'), 'Controlled')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      chronic_conditions: [
        { name: 'Type 2 Diabetes', since_year: 2015 },
        { name: 'Hypertension', since_year: 2020, notes: 'Controlled' },
      ],
    })
  })

  it('sends a new medication with only the parts that were filled in', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add medication' }))
    const medication = row(dialog, 'Medication 2')
    await user.type(medication.getByRole('textbox', { name: /Medication 2/ }), 'Atorvastatin')
    await user.type(medication.getByLabelText('Dosage'), '10mg')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({
      current_medications: [
        { name: 'Metformin', dosage: '500mg', frequency: 'Twice daily', started_on: '2015-06-01' },
        { name: 'Atorvastatin', dosage: '10mg' },
      ],
    })
  })

  it('treats a row added and removed again as no change', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add allergy' }))
    expect(saveButton(dialog)).toBeEnabled()
    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 3' }))

    expect(saveButton(dialog)).toBeDisabled()
    expect(patches()).toHaveLength(0)
  })
})

describe('validation before any request', () => {
  it.each([
    ['a blank first name', field('Identity', /First name/), '', 'First name is required'],
    ['a blank last name', field('Identity', /Last name/), '', 'Last name is required'],
    [
      'a date of birth in the future',
      field('Identity', /Date of birth/),
      '2099-01-01',
      'Enter a date in the past, within the last 130 years',
    ],
    [
      'a date of birth over 130 years ago',
      field('Identity', /Date of birth/),
      '1850-01-01',
      'Enter a date in the past, within the last 130 years',
    ],
    ['a phone that is not a number', field('Contact', 'Phone'), 'ask at the desk', PHONE_FORMAT],
    // The API adds +91 to a bare Indian mobile and to nothing else.
    ['a landline without its country code', field('Contact', 'Phone'), '0484 2345678', PHONE_FORMAT],
    ['a country code without its +', field('Contact', 'Phone'), '91 98123 45678', PHONE_FORMAT],
    ['a foreign number without its country code', field('Contact', 'Phone'), '415 555 2671', PHONE_FORMAT],
    ['a malformed email', field('Contact', 'Email'), 'thomas-at-example', 'Enter a valid email'],
    ['an email with an empty domain label', field('Contact', 'Email'), 'thomas@example..com', 'Enter a valid email'],
    ['an email whose domain ends in a dot', field('Contact', 'Email'), 'thomas@example.com.', 'Enter a valid email'],
    ['an email whose domain starts with a dot', field('Contact', 'Email'), 'thomas@.example.com', 'Enter a valid email'],
    [
      'an emergency contact phone that is not a number',
      field('Emergency contact', 'Phone'),
      'unknown',
      PHONE_FORMAT,
    ],
    [
      'an emergency contact landline without its country code',
      field('Emergency contact', 'Phone'),
      '040 2345 6789',
      PHONE_FORMAT,
    ],
    ['a marital status over 20 characters', field('Identity', 'Marital status'), 'x'.repeat(21), 'Use at most 20 characters'],
  ])('blocks %s', async (_case, find, value, message) => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const input = find(dialog)

    await retype(user, input, value)
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText(message)).toBeInTheDocument()
    expect(input).toHaveAccessibleDescription(message)
    expect(patches()).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('accepts a phone with its country code and an email on a short domain, as the API does', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Contact').getByLabelText('Phone'), '+1 415-555-2671')
    await retype(user, group(dialog, 'Contact').getByLabelText('Email'), 'a@b.c')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ phone: '+1 415-555-2671', email: 'a@b.c' })
  })

  it('blocks a history row left without a name', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add allergy' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add condition' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add medication' }))
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText('Name the allergen, or remove this row')).toBeInTheDocument()
    expect(row(dialog, 'Allergy 3').getByRole('textbox', { name: /Allergy 3/ })).toHaveAccessibleDescription(
      'Name the allergen, or remove this row',
    )
    expect(within(dialog).getByText('Name the condition, or remove this row')).toBeInTheDocument()
    expect(within(dialog).getByText('Name the medication, or remove this row')).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })

  it.each(['1899', '2999', 'last year'])('blocks the onset year "%s"', async (year) => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, row(dialog, 'Condition 1').getByLabelText('Since year'), year)
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText('Enter a year from 1900 to this year')).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })
})

describe('length limits', () => {
  const chars = (length: number) => 'x'.repeat(length)
  const valueAt = (body: unknown, path: (string | number)[]) =>
    path.reduce<unknown>((value, key) => (value as Record<string | number, unknown>)[key], body)

  type Limit = [
    name: string,
    find: (dialog: HTMLElement) => HTMLElement,
    max: number,
    path: (string | number)[],
    make?: (length: number) => string,
  ]

  // Every `max_length` in backend/app/schemas/patient.py that the form can
  // reach. The numbers are restated here, not imported from the form's schema:
  // this is what holds the two to each other.
  const LIMITS: Limit[] = [
    ['first name', field('Identity', /First name/), 100, ['first_name']],
    ['last name', field('Identity', /Last name/), 100, ['last_name']],
    ['marital status', field('Identity', 'Marital status'), 20, ['marital_status']],
    ['occupation', field('Identity', 'Occupation'), 100, ['occupation']],
    ['email', field('Contact', 'Email'), 200, ['email'], (length) => `${chars(length - 12)}@example.com`],
    ['address line 1', field('Address', 'Line 1'), 200, ['address', 'line1']],
    ['address line 2', field('Address', 'Line 2'), 200, ['address', 'line2']],
    ['city', field('Address', 'City'), 100, ['address', 'city']],
    ['state', field('Address', 'State'), 100, ['address', 'state']],
    ['postal code', field('Address', 'Postal code'), 20, ['address', 'postal_code']],
    ['emergency contact name', field('Emergency contact', 'Name'), 200, ['emergency_contact', 'name']],
    ['relationship', field('Emergency contact', 'Relationship'), 50, ['emergency_contact', 'relation']],
    ['allergy name', rowField('Allergy 1'), 200, ['allergies', 0, 'name']],
    ['allergy reaction', rowField('Allergy 1', 'Reaction'), 500, ['allergies', 0, 'reaction']],
    ['condition name', rowField('Condition 1'), 200, ['chronic_conditions', 0, 'name']],
    ['condition notes', rowField('Condition 1', 'Notes'), 1000, ['chronic_conditions', 0, 'notes']],
    ['medication name', rowField('Medication 1'), 200, ['current_medications', 0, 'name']],
    ['medication dosage', rowField('Medication 1', 'Dosage'), 100, ['current_medications', 0, 'dosage']],
    ['medication frequency', rowField('Medication 1', 'Frequency'), 100, ['current_medications', 0, 'frequency']],
    ['administrative notes', field('Notes', 'Administrative notes'), 5000, ['notes']],
  ]

  it.each(LIMITS)('%s: blocks one character over the limit, and sends exactly the limit', async (...limit) => {
    const [, find, max, path, make = chars] = limit
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    const input = find(dialog)

    await fill(user, input, make(max + 1))
    await user.click(saveButton(dialog))

    await waitFor(() => expect(input).toHaveAccessibleDescription(`Use at most ${max} characters`))
    expect(patches()).toHaveLength(0)

    await fill(user, input, make(max))
    const request = await save(user, dialog)

    expect(valueAt(bodyOf(request), path)).toBe(make(max))
  })
})

describe('when the API refuses the update', () => {
  async function submitAChange(user: User) {
    const dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await user.click(saveButton(dialog))
    await waitFor(() => expect(patches()).toHaveLength(1))
    return dialog
  }

  it('shows a 422 under the nested field it names', async () => {
    onPatch = () =>
      invalid([
        {
          field: 'emergency_contact.phone',
          message: 'Value error, Phone must be in E.164 format, e.g. +919812345678.',
        },
      ])
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)

    const phone = group(dialog, 'Emergency contact').getByLabelText('Phone')
    await waitFor(() =>
      expect(phone).toHaveAccessibleDescription('Phone must be in E.164 format, e.g. +919812345678.'),
    )
    expect(phone).toBeInvalid()
    // Not repeated above the form, and the record on the page is untouched.
    expect(within(dialog).queryByText("That didn't go through")).not.toBeInTheDocument()
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    // The field is four sections above Save: the user is told, and taken to it.
    expect(toastError).toHaveBeenCalledExactlyOnceWith("Couldn't save. Check the highlighted fields.")
    await waitFor(() => expect(phone).toHaveFocus())
  })

  it('does not go back to a refused field when the form is opened again', async () => {
    onPatch = () => invalid([{ field: 'emergency_contact.phone', message: 'Value error, Not reachable.' }])
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)
    const phone = () => group(screen.getByRole('dialog'), 'Emergency contact').getByLabelText('Phone')
    await waitFor(() => expect(phone()).toHaveFocus())

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await openEdit(user)
    // Long enough for a deferred focus call to have landed.
    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(phone()).toHaveValue('+919812300000')
    expect(phone()).not.toHaveFocus()
  })

  it('shows a 422 under the list row it names, in the nested error shape too', async () => {
    onPatch = () =>
      invalid(
        { errors: [{ field: 'allergies.1.name', message: 'String should have at most 200 characters' }] },
        'Patient data failed validation.',
      )
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)

    const latex = row(dialog, 'Allergy 2').getByRole('textbox', { name: /Allergy 2/ })
    await waitFor(() =>
      expect(latex).toHaveAccessibleDescription('String should have at most 200 characters'),
    )
    expect(row(dialog, 'Allergy 1').getByRole('textbox', { name: /Allergy 1/ })).toBeValid()
    await waitFor(() => expect(latex).toHaveFocus())
  })

  it('shows a whole-body 422, which names no field, above the form', async () => {
    onPatch = () =>
      invalid([{ field: '', message: 'Value error, These fields cannot be set to null: gender.' }])
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)

    const alert = await within(dialog).findByText("That didn't go through")
    expect(alert.closest('[role="alert"]')).toHaveTextContent(
      'These fields cannot be set to null: gender.',
    )
    expect(toastError).toHaveBeenCalledExactlyOnceWith('These fields cannot be set to null: gender.')
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('shows a 403 in the dialog, in the API\'s words', async () => {
    onPatch = () =>
      fail(403, 'Permission denied. Required: patient.update.', {
        error_code: 'PERMISSION_DENIED',
        errors: null,
      })
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)

    expect(
      await within(dialog).findByText('Permission denied. Required: patient.update.'),
    ).toBeInTheDocument()
    // The message is above the first field; the toast is where Save is.
    expect(toastError).toHaveBeenCalledExactlyOnceWith('Permission denied. Required: patient.update.')
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('shows a 404 in the dialog, then stops offering a record that is gone', async () => {
    let finishRead: () => void = () => {}
    // Deactivated by someone else since the page loaded: reads fail from now on too.
    onPatch = () => {
      onRead = () => new Promise<Outcome>((resolve) => (finishRead = () => resolve(notFound())))
      return notFound()
    }
    const user = userEvent.setup()
    renderPatient(ADMIN)
    const dialog = await submitAChange(user)

    expect(await within(dialog).findByText('Patient not found.')).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledExactlyOnceWith('Patient not found.')
    expect(toastSuccess).not.toHaveBeenCalled()

    // The refusal sends the page back to the server for the record.
    await waitFor(() => expect(reads()).toHaveLength(2))
    finishRead()
    expect(await screen.findByText("Couldn't load this patient")).toBeInTheDocument()
    expect(editButton()).not.toBeInTheDocument()
  })

  it('shows a plain message, not internal text, when the server fails', async () => {
    onPatch = () => fail(500, 'Traceback (most recent call last): asyncpg…', { error_code: 'INTERNAL_ERROR' })
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await submitAChange(user)

    expect(await within(dialog).findByText("Couldn't save the changes. Please try again.")).toBeInTheDocument()
    expect(within(dialog).queryByText(/Traceback/)).not.toBeInTheDocument()
    expect(toastError).toHaveBeenCalledExactlyOnceWith("Couldn't save the changes. Please try again.")

    // What was typed is still there, and saving again goes through.
    onPatch = null
    await user.click(saveButton(dialog))
    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved changes to Thomas George'))
    expect(bodyOf(patches()[1])).toEqual({ occupation: 'Headteacher' })
  })
})

describe('when the record changed after the page loaded it', () => {
  const peanuts = { name: 'Peanuts', severity: 'severe' }
  const patientRequests = () =>
    api.sent.filter((c) => c.url === `/patients/${ID}`).map((c) => c.method)

  it('refuses to save an allergy list that would drop an allergy somebody else added', async () => {
    const user = userEvent.setup()
    renderPatient(DOCTOR)
    const dialog = await openEdit(user)

    // A nurse records an allergy from another workstation. Nothing tells this page.
    patient.allergies = [...patient.allergies, peanuts]
    await retype(user, row(dialog, 'Allergy 1').getByLabelText('Reaction'), 'Anaphylaxis')
    await user.click(saveButton(dialog))

    const refusal =
      'The allergies on this record changed after this form was opened, and saving would undo ' +
      'that change. Close the form and open it again to edit the current record.'
    expect(await within(dialog).findByText(refusal)).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledExactlyOnceWith(refusal)
    expect(toastSuccess).not.toHaveBeenCalled()
    expect(patches()).toHaveLength(0)
    expect(patient.allergies).toContainEqual(peanuts)

    // Reopened, the form holds the current list and the edit goes on top of it.
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Medical history', 'Allergies')).toHaveTextContent(
      'Penicillin (severe), Latex, Peanuts (severe)',
    )
    const reopened = await openEdit(user)
    expect(group(reopened, 'Allergies').getAllByRole('listitem')).toHaveLength(3)
    await retype(user, row(reopened, 'Allergy 1').getByLabelText('Reaction'), 'Anaphylaxis')
    const request = await save(user, reopened)

    expect(bodyOf(request)).toEqual({
      allergies: [
        { name: 'Penicillin', severity: 'severe', reaction: 'Anaphylaxis', noted_on: '2019-04-11' },
        { name: 'Latex', severity: 'moderate' },
        peanuts,
      ],
    })
  })

  it.each<[string, () => void, (dialog: HTMLElement) => HTMLElement, string]>([
    [
      'address',
      () => (patient.address = { ...patient.address, line2: 'Flat 3B' }),
      field('Address', 'City'),
      'Ernakulam',
    ],
    [
      'emergency contact',
      () => (patient.emergency_contact = { ...patient.emergency_contact, phone: '+919800000002' }),
      field('Emergency contact', 'Relationship'),
      'Wife',
    ],
    [
      'chronic conditions',
      () => (patient.chronic_conditions = [...patient.chronic_conditions, { name: 'Asthma' }]),
      rowField('Condition 1', 'Notes'),
      'Diet controlled',
    ],
    [
      'current medications',
      () => (patient.current_medications = []),
      rowField('Medication 1', 'Dosage'),
      '850mg',
    ],
  ])('refuses to save over a changed %s', async (part, changeElsewhere, find, value) => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    changeElsewhere()
    await retype(user, find(dialog), value)
    await user.click(saveButton(dialog))

    expect(
      await within(dialog).findByText(new RegExp(`^The ${part} on this record changed after`)),
    ).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('names every part that would be overwritten', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    patient.allergies = [...patient.allergies, peanuts]
    patient.current_medications = []
    patient.address = null
    await retype(user, row(dialog, 'Allergy 1').getByLabelText('Reaction'), 'Anaphylaxis')
    await retype(user, row(dialog, 'Medication 1').getByLabelText('Dosage'), '850mg')
    await retype(user, group(dialog, 'Address').getByLabelText('City'), 'Ernakulam')
    await user.click(saveButton(dialog))

    expect(
      await within(dialog).findByText(
        /^The address, allergies and current medications on this record changed after/,
      ),
    ).toBeInTheDocument()
    expect(patches()).toHaveLength(0)
  })

  it('reads the stored record just before replacing a list, and not for a plain field', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    let dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await save(user, dialog)
    expect(patientRequests()).toEqual(['get', 'patch'])
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    dialog = await openEdit(user)
    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 2' }))
    await user.click(saveButton(dialog))

    await waitFor(() => expect(patches()).toHaveLength(2))
    expect(patientRequests()).toEqual(['get', 'patch', 'get', 'patch'])
  })

  it('still saves the user\'s own fields when the list somebody else changed is not being sent', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    patient.allergies = [...patient.allergies, peanuts]
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ occupation: 'Headteacher' })
    expect(patient.allergies).toContainEqual(peanuts)
  })

  it('lets a save be retried when the first attempt was stored but its answer was lost', async () => {
    // The update lands, the response does not: the stored list is no longer
    // the one the form opened with, but it is the user's own.
    onPatch = (config) => {
      applyPatch(config)
      onPatch = null
      return fail(502, 'Bad Gateway', { error_code: 'INTERNAL_ERROR' })
    }
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await user.click(within(dialog).getByRole('button', { name: 'Add allergy' }))
    await user.type(row(dialog, 'Allergy 3').getByRole('textbox', { name: /Allergy 3/ }), 'Peanuts')
    await user.click(saveButton(dialog))
    expect(await within(dialog).findByText("Couldn't save the changes. Please try again.")).toBeInTheDocument()

    await user.click(saveButton(dialog))

    await waitFor(() => expect(toastSuccess).toHaveBeenCalledWith('Saved changes to Thomas George'))
    expect(patches()).toHaveLength(2)
    expect(within(dialog).queryByText(/on this record changed after/)).not.toBeInTheDocument()
  })

  it('sends nothing when the stored record cannot be read, and saves once it can', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    onRead = unavailable
    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 2' }))
    await user.click(saveButton(dialog))

    expect(await within(dialog).findByText("Couldn't save the changes. Please try again.")).toBeInTheDocument()
    expect(toastError).toHaveBeenCalledExactlyOnceWith("Couldn't save the changes. Please try again.")
    expect(patches()).toHaveLength(0)
    // The form is still there with the change in it.
    expect(group(dialog, 'Allergies').getAllByRole('listitem')).toHaveLength(1)

    onRead = null
    const request = await save(user, dialog)
    expect(bodyOf(request)).toEqual({
      allergies: [{ name: 'Penicillin', severity: 'severe', reaction: 'Hives', noted_on: '2019-04-11' }],
    })
  })

  it('sends nothing, and stops offering the record, when it was deactivated before a list was saved', async () => {
    const user = userEvent.setup()
    renderPatient(ADMIN)
    const dialog = await openEdit(user)

    onRead = notFound
    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 2' }))
    await user.click(saveButton(dialog))

    expect(await screen.findByText("Couldn't load this patient")).toBeInTheDocument()
    await waitFor(() => expect(toastError).toHaveBeenCalledExactlyOnceWith('Patient not found.'))
    expect(patches()).toHaveLength(0)
    expect(editButton()).not.toBeInTheDocument()
  })

  it('keeps the open form, and what was typed, when a refresh of the record fails', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await user.click(within(dialog).getByRole('button', { name: 'Add medication' }))
    await user.type(row(dialog, 'Medication 2').getByRole('textbox', { name: /Medication 2/ }), 'Insulin')

    // The ward network drops: the stale record is refetched and the request fails.
    onRead = unavailable
    await refresh()

    expect(screen.getByRole('dialog')).toBe(dialog)
    expect(group(dialog, 'Identity').getByLabelText('Occupation')).toHaveValue('Headteacher')
    expect(row(dialog, 'Medication 2').getByRole('textbox', { name: /Medication 2/ })).toHaveValue('Insulin')

    onRead = null
    const request = await save(user, dialog)
    expect(bodyOf(request)).toEqual({
      occupation: 'Headteacher',
      current_medications: [
        { name: 'Metformin', dosage: '500mg', frequency: 'Twice daily', started_on: '2015-06-01' },
        { name: 'Insulin' },
      ],
    })
  })
})

describe('removing a history row from the keyboard', () => {
  const removeButton = (dialog: HTMLElement, name: string) => within(dialog).getByRole('button', { name })

  it('moves focus to the row that takes the removed one\'s place', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    removeButton(dialog, 'Remove allergy 1').focus()
    await user.keyboard('{Enter}')

    expect(row(dialog, 'Allergy 1').getByRole('textbox', { name: /Allergy 1/ })).toHaveValue('Latex')
    expect(removeButton(dialog, 'Remove allergy 1')).toHaveFocus()
  })

  it('moves focus to the row before when the last one is removed', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    removeButton(dialog, 'Remove allergy 2').focus()
    await user.keyboard('{Enter}')

    expect(group(dialog, 'Allergies').getAllByRole('listitem')).toHaveLength(1)
    expect(removeButton(dialog, 'Remove allergy 1')).toHaveFocus()
  })

  it('moves focus to the Add button when the list is emptied, so Tab carries on from there', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    removeButton(dialog, 'Remove medication 1').focus()
    await user.keyboard('{Enter}')

    expect(within(dialog).getByText('No current medications recorded.')).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Add medication' })).toHaveFocus()
    // Not back at the top of the form, thirty fields away.
    await user.tab()
    expect(group(dialog, 'Notes').getByLabelText('Administrative notes')).toHaveFocus()
  })
})

describe('cancelling', () => {
  it('sends nothing, leaves the record alone, and reopens on the original values', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    let dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText(/First name/), 'Tom')
    await user.clear(group(dialog, 'Address').getByLabelText('City'))
    await user.click(within(dialog).getByRole('button', { name: 'Remove allergy 1' }))
    await user.click(within(dialog).getByRole('button', { name: 'Add medication' }))
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.sent.filter((c) => c.method !== 'get')).toHaveLength(0)
    expect(screen.getByRole('heading', { name: 'Thomas George' })).toBeInTheDocument()
    expect(shown('Medical history', 'Allergies')).toHaveTextContent('Penicillin (severe), Latex')

    dialog = await openEdit(user)
    expect(group(dialog, 'Identity').getByLabelText(/First name/)).toHaveValue('Thomas')
    expect(group(dialog, 'Address').getByLabelText('City')).toHaveValue('Kochi')
    expect(group(dialog, 'Allergies').getAllByRole('listitem')).toHaveLength(2)
    expect(row(dialog, 'Allergy 1').getByRole('textbox', { name: /Allergy 1/ })).toHaveValue('Penicillin')
    expect(group(dialog, 'Current medications').getAllByRole('listitem')).toHaveLength(1)
    expect(saveButton(dialog)).toBeDisabled()
  })
})

describe('after a save', () => {
  it('shows the values as the server stored them, and edits from those next time', async () => {
    // The API normalizes the phone to E.164 and lowercases the email.
    stored = { phone: '+919800011122', email: 'thomas.g@example.com' }
    const user = userEvent.setup()
    renderPatient(NURSE)
    let dialog = await openEdit(user)

    await retype(user, group(dialog, 'Contact').getByLabelText('Phone'), '98000 11122')
    await retype(user, group(dialog, 'Contact').getByLabelText('Email'), 'Thomas.G@Example.com')
    const request = await save(user, dialog)

    // Sent as typed: normalizing is the server's job.
    expect(bodyOf(request)).toEqual({ phone: '98000 11122', email: 'Thomas.G@Example.com' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Contact', 'Phone')).toHaveTextContent('+919800011122')
    expect(shown('Contact', 'Email')).toHaveTextContent('thomas.g@example.com')

    dialog = await openEdit(user)
    expect(group(dialog, 'Contact').getByLabelText('Phone')).toHaveValue('+919800011122')
    expect(saveButton(dialog)).toBeDisabled()
  })

  it('measures a second edit against the saved record, not the one first loaded', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    let dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await save(user, dialog)
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Marital status'), 'Widowed')
    await user.click(saveButton(dialog))

    await waitFor(() => expect(patches()).toHaveLength(2))
    expect(bodyOf(patches()[1])).toEqual({ marital_status: 'Widowed' })
  })

  it('serves the saved record to the next visit', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await save(user, dialog)

    await act(() => client.invalidateQueries({ queryKey: patientKeys.detail(ID) }))

    expect(reads()).toHaveLength(2)
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Headteacher')
  })

  it('refreshes the lists that carry the patient\'s name, but not the record it just received', async () => {
    const registry = patientKeys.list({ page: 1 })
    const appointments = appointmentKeys.list({})
    const invoices = billingKeys.list({})
    for (const key of [registry, appointments, invoices]) client.setQueryData(key, { items: [] })
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    await retype(user, group(dialog, 'Identity').getByLabelText(/Last name/), 'Varghese')
    await save(user, dialog)

    for (const key of [registry, appointments, invoices]) {
      expect(client.getQueryState(key)?.isInvalidated).toBe(true)
    }
    expect(client.getQueryState(patientKeys.detail(ID))?.isInvalidated).toBe(false)
  })

  it('reloads the invoices shown on the page, which carry the name', async () => {
    const invoiceReads = () => api.requests('get', '/invoices')
    const user = userEvent.setup()
    renderPatient([...NURSE, 'invoice.read'])
    const dialog = await openEdit(user)
    await waitFor(() => expect(invoiceReads()).toHaveLength(1))

    await retype(user, group(dialog, 'Identity').getByLabelText(/Last name/), 'Varghese')
    await save(user, dialog)

    await waitFor(() => expect(invoiceReads()).toHaveLength(2))
  })

  it('does not send back a field that somebody else changed while the form was open', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)

    // A colleague corrects the phone; the page behind the dialog picks it up.
    patient.phone = '+919800000001'
    await act(() => client.invalidateQueries({ queryKey: patientKeys.detail(ID) }))

    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    const request = await save(user, dialog)

    expect(bodyOf(request)).toEqual({ occupation: 'Headteacher' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(shown('Contact', 'Phone')).toHaveTextContent('+919800000001')
  })
})

describe('patient registry', () => {
  it('links each row to its patient', async () => {
    signIn(RECEPTIONIST)
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <PatientsPage />
        </QueryClientProvider>
      </MemoryRouter>,
    )

    const view = await screen.findByRole('link', { name: 'View Thomas George, MRN-2026-00042' })
    expect(view).toHaveAttribute('href', `/patients/${ID}`)
    expect(within(view.closest('tr') as HTMLElement).getByText('MRN-2026-00042')).toBeInTheDocument()
  })

  it('tells two patients with the same name apart by MRN in the link names', async () => {
    const namesake = thomas({ id: '3f6c1b2e-0000-4000-8000-000000000002', mrn: 'MRN-2026-00107' })
    registry = [patient, namesake]
    signIn(RECEPTIONIST)
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <PatientsPage />
        </QueryClientProvider>
      </MemoryRouter>,
    )

    // Each name finds exactly one link: a query by role fails on a second match.
    const first = await screen.findByRole('link', { name: 'View Thomas George, MRN-2026-00042' })
    const second = screen.getByRole('link', { name: 'View Thomas George, MRN-2026-00107' })
    expect(first).toHaveAttribute('href', `/patients/${ID}`)
    expect(second).toHaveAttribute('href', `/patients/${namesake.id}`)
  })
})

describe('clicking outside the form', () => {
  const overlay = () => document.querySelector('[data-state="open"].fixed.inset-0') as HTMLElement

  it('closes a form nothing was typed into', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    await openEdit(user)
    await user.click(overlay())
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('keeps a form with unsaved changes open; Escape still closes it', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    const dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await user.click(overlay())
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    expect(group(dialog, 'Identity').getByLabelText('Occupation')).toHaveValue('Headteacher')
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

describe('a read that lands after a write', () => {
  it('does not put the old record back over a saved edit', async () => {
    const user = userEvent.setup()
    renderPatient(NURSE)
    await loaded()
    const answer = ok(structuredClone(patient))
    let arrive: () => void = () => {}
    onRead = () => new Promise<Outcome>((resolve) => (arrive = () => resolve(answer)))
    const before = reads().length
    act(() => {
      void client.invalidateQueries({ queryKey: patientKeys.detail(ID) })
    })
    await waitFor(() => expect(reads()).toHaveLength(before + 1))
    onRead = null

    const dialog = await openEdit(user)
    await retype(user, group(dialog, 'Identity').getByLabelText('Occupation'), 'Headteacher')
    await save(user, dialog)
    await waitFor(() => expect(shown('Demographics', 'Occupation')).toHaveTextContent('Headteacher'))
    await act(async () => {
      arrive()
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(shown('Demographics', 'Occupation')).toHaveTextContent('Headteacher')
  })
})
