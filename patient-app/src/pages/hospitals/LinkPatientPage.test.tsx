import { screen, waitFor } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { bodyOf, fail, headerOf, ok, serve } from '@/test/fakeApi'
import { cityCare, me } from '@/test/fixtures'
import { renderApp, signIn } from '@/test/renderApp'

const LINK = 'POST /hospitals/city-care/link'
const REGISTER = 'POST /hospitals/city-care/register'
const ME = 'GET /me'
const DOB = '1990-05-17'
const CONSENT = '2026-10-draft'

const linkBody = { hospital_id: 'hosp-1', hospital_name: 'City Care Hospital', linked_at: '2026-10-07T08:00:00Z' }
// The server's exact wording (backend record_link_service / consent_service).
const ALREADY_LINKED = 'You are already linked to a record at this hospital.'
const RECORD_EXISTS = 'A record may already exist for you at this hospital. Please link to it instead.'
const ALREADY_REGISTERED = 'You have already registered at this hospital.'
const STALE_CONSENT = 'The policy has changed. Please review the current version and try again.'
const conflict = (message: string) => fail(409, 'RESOURCE_CONFLICT', message)
const RELOAD = /^Please reload and try again\.$/

const notFound = () => fail(404, 'RESOURCE_NOT_FOUND', 'server wording that must not be shown')

const codeField = () => screen.findByLabelText('Hospital code')
const dobField = () => screen.getByLabelText('Date of birth')
const consentBox = () => screen.getByRole('checkbox')
const findMyRecord = () => screen.getByRole('button', { name: 'Find my record' })

/** Fill the details form and submit it. */
async function submitDetails(user: UserEvent, { code = 'city-care', dob = DOB, consent = true } = {}) {
  await user.type(await codeField(), code)
  await user.type(dobField(), dob)
  if (consent) await user.click(consentBox())
  await user.click(findMyRecord())
}

async function fillRegistration(user: UserEvent) {
  await user.type(screen.getByLabelText('First name'), 'Asha')
  await user.type(screen.getByLabelText('Last name'), 'Rao')
  await user.selectOptions(screen.getByLabelText('Gender'), 'Female')
  await user.click(screen.getByRole('checkbox'))
  await user.click(screen.getByRole('button', { name: 'Create my record' }))
}

function open() {
  signIn('access-1')
  return renderApp('/link-patient')
}

describe('link a hospital record', () => {
  it('validates the form before calling the server', async () => {
    const api = serve({})
    const { user } = open()
    await codeField()

    await user.click(findMyRecord())

    expect(await screen.findByText('Enter the hospital code.')).toBeInTheDocument()
    expect(screen.getByText('Enter your date of birth.')).toBeInTheDocument()
    expect(screen.getByText('Please tick the box to continue.')).toBeInTheDocument()
    expect(await codeField()).toHaveAttribute('aria-invalid', 'true')
    expect(dobField()).toHaveAccessibleDescription('Enter your date of birth.')
    expect(consentBox()).toHaveAttribute('aria-invalid', 'true')
    expect(api.sent).toHaveLength(0)
  })

  it('refuses a date of birth in the future', async () => {
    const api = serve({})
    const { user } = open()

    await submitDetails(user, { dob: '2999-01-01' })

    expect(await screen.findByText('Your date of birth cannot be in the future.')).toBeInTheDocument()
    expect(api.sent).toHaveLength(0)
  })

  it('LINKED — creates the link, sending only the date of birth and consent, never a phone or patient id', async () => {
    const api = serve({ [LINK]: ok(linkBody, 201), [ME]: ok(me([cityCare])) })
    const { user } = open()

    await submitDetails(user)

    expect(await screen.findByRole('heading', { name: 'Your record is linked' })).toBeInTheDocument()

    const [request] = api.calls(LINK)
    expect(api.calls(LINK)).toHaveLength(1)
    expect(bodyOf(request)).toEqual({ date_of_birth: DOB, consent_policy_version: CONSENT })
    expect(headerOf(request, 'Authorization')).toBe('Bearer access-1')

    // Home shows the new hospital without a reload.
    await user.click(screen.getByRole('link', { name: 'Go to home' }))
    expect(await screen.findByText('City Care Hospital')).toBeInTheDocument()
  })

  it('escapes the hospital code so it cannot reach another path', async () => {
    const api = serve({ 'POST /hospitals/..%2F..%2Fme%3Fx/link': notFound() })
    const { user } = open()

    await submitDetails(user, { code: '../../me?x' })

    expect(await screen.findByText('We could not find a record with these details.')).toBeInTheDocument()
    expect(api.sent.map((c) => c.url)).toEqual(['/hospitals/..%2F..%2Fme%3Fx/link'])
  })

  it('ALREADY LINKED — a 200 is reported as nothing having changed', async () => {
    serve({ [LINK]: ok(linkBody, 200), [ME]: ok(me([cityCare])) })
    const { user } = open()

    await submitDetails(user)

    expect(await screen.findByRole('heading', { name: 'This record is already linked' })).toBeInTheDocument()
    expect(screen.queryByText('Your record is linked')).not.toBeInTheDocument()
  })

  it('MRN REQUIRED then success — asks for the MRN only after the server does, then links with it', async () => {
    const api = serve({ [LINK]: fail(409, 'LINK_MRN_REQUIRED'), [ME]: ok(me([cityCare])) })
    const { user } = open()
    await codeField()
    expect(screen.queryByLabelText(/MRN/)).not.toBeInTheDocument()

    await submitDetails(user)

    const mrn = await screen.findByLabelText('Medical record number (MRN)')
    expect(screen.getByText('We need your MRN to find your record. Please add it below.')).toBeInTheDocument()
    // Needing an MRN is not "no record": registration is not offered.
    expect(screen.queryByRole('button', { name: 'Register as a new patient' })).not.toBeInTheDocument()

    // Submitting without it is caught here, not sent again.
    await user.click(findMyRecord())
    expect(await screen.findByText('Enter your MRN.')).toBeInTheDocument()
    expect(api.calls(LINK)).toHaveLength(1)

    api.on({ [LINK]: ok(linkBody, 201) })
    await user.type(mrn, 'MRN-00042')
    await user.click(findMyRecord())

    expect(await screen.findByRole('heading', { name: 'Your record is linked' })).toBeInTheDocument()
    expect(bodyOf(api.calls(LINK)[1])).toEqual({
      date_of_birth: DOB,
      mrn: 'MRN-00042',
      consent_policy_version: CONSENT,
    })
  })

  it('MRN REQUIRED then wrong MRN — the neutral message, and no offer to register', async () => {
    const api = serve({ [LINK]: fail(409, 'LINK_MRN_REQUIRED') })
    const { user } = open()
    await submitDetails(user)

    api.on({ [LINK]: notFound() })
    await user.type(await screen.findByLabelText('Medical record number (MRN)'), 'MRN-WRONG')
    await user.click(findMyRecord())

    expect(await screen.findByText('We could not find a record with these details.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Register as a new patient' })).not.toBeInTheDocument()
  })

  it('MRN REQUIRED — the MRN is dropped again when the patient changes the details it was asked for', async () => {
    const api = serve({ [LINK]: fail(409, 'LINK_MRN_REQUIRED') })
    const { user } = open()
    await submitDetails(user)
    await user.type(await screen.findByLabelText('Medical record number (MRN)'), 'MRN-00042')

    api.on({ 'POST /hospitals/city-care-2/link': ok(linkBody, 201), [ME]: ok(me([cityCare])) })
    await user.type(await codeField(), '-2')
    expect(screen.queryByLabelText(/MRN/)).not.toBeInTheDocument()
    await user.click(findMyRecord())

    expect(await screen.findByRole('heading', { name: 'Your record is linked' })).toBeInTheDocument()
    expect(bodyOf(api.calls('POST /hospitals/city-care-2/link')[0])).toEqual({
      date_of_birth: DOB,
      consent_policy_version: CONSENT,
    })
  })

  it('NOT FOUND -> REGISTER CONFIRMATION -> REGISTERED', async () => {
    const api = serve({ [LINK]: notFound(), [REGISTER]: ok({ link: linkBody }, 201), [ME]: ok(me([cityCare])) })
    const { user } = open()

    await submitDetails(user)

    // One neutral message: nothing says whether the phone, the date or the hospital was the miss.
    expect(await screen.findByText('We could not find a record with these details.')).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('server wording')
    await user.click(screen.getByRole('button', { name: 'Register as a new patient' }))

    // The confirmation step shows the date of birth back before anything is created.
    expect(await screen.findByRole('heading', { name: 'Register as a new patient' })).toBeInTheDocument()
    expect(screen.getByText('17 May 1990')).toBeInTheDocument()
    expect(screen.getByText('city-care')).toBeInTheDocument()
    expect(api.calls(REGISTER)).toHaveLength(0)

    // Its own validation first.
    await user.click(screen.getByRole('button', { name: 'Create my record' }))
    expect(await screen.findByText('Enter your first name.')).toBeInTheDocument()
    expect(screen.getByText('Enter your last name.')).toBeInTheDocument()
    expect(screen.getByText('Choose an option.')).toBeInTheDocument()
    expect(screen.getByText('Please tick the box to continue.')).toBeInTheDocument()
    expect(api.calls(REGISTER)).toHaveLength(0)

    await fillRegistration(user)

    expect(await screen.findByRole('heading', { name: 'You are registered' })).toBeInTheDocument()
    const [request] = api.calls(REGISTER)
    // The phone comes from the account on the server; the client cannot supply one.
    expect(bodyOf(request)).toEqual({
      first_name: 'Asha',
      last_name: 'Rao',
      date_of_birth: DOB,
      gender: 'female',
      consent_policy_version: CONSENT,
    })
    expect(headerOf(request, 'Authorization')).toBe('Bearer access-1')

    await user.click(screen.getByRole('link', { name: 'Go to home' }))
    expect(await screen.findByText('City Care Hospital')).toBeInTheDocument()
  })

  it('NOT FOUND -> REGISTER — the patient can go back and correct a mistyped date instead', async () => {
    serve({ [LINK]: notFound() })
    const { user } = open()
    await submitDetails(user)
    await user.click(await screen.findByRole('button', { name: 'Register as a new patient' }))

    await user.click(await screen.findByRole('button', { name: 'These are not right — go back' }))

    expect(await screen.findByRole('heading', { name: 'Link your hospital record' })).toBeInTheDocument()
  })

  it.each([
    ['a record now exists (409)', conflict(RECORD_EXISTS), /a record may already exist for you/],
    ['the account is already linked here (409)', conflict(ALREADY_LINKED), /a record may already exist for you/],
    ['the account already registered here (409)', conflict(ALREADY_REGISTERED), /a record may already exist for you/],
    ['the consent version is stale (409)', conflict(STALE_CONSENT), RELOAD],
    ['a conflict this build does not know (409)', conflict('Something new.'), RELOAD],
    ['a platform policy is pending (403)', fail(403, 'CONSENT_REQUIRED'), /^We cannot complete this in the app right now/],
    ['the details fail the patient rules (422)', fail(422, 'VALIDATION_ERROR'), /Some of these details are not valid/],
    ['the record is deactivated (403)', fail(403, 'LINK_UNAVAILABLE'), /^Please contact the hospital\.$/],
    ['the hospital is unknown (404)', fail(404, 'RESOURCE_NOT_FOUND'), /Check the hospital code, or contact the hospital/],
    ['the server fails (500)', fail(500, 'INTERNAL_ERROR'), /Something went wrong/],
  ])('REGISTER refused because %s', async (_case, outcome, message) => {
    serve({ [LINK]: notFound(), [REGISTER]: outcome })
    const { user } = open()
    await submitDetails(user)
    await user.click(await screen.findByRole('button', { name: 'Register as a new patient' }))
    await screen.findByRole('heading', { name: 'Register as a new patient' })

    await fillRegistration(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.queryByText('You are registered')).not.toBeInTheDocument()
  })

  it('UNAVAILABLE — tells the patient to contact the hospital and offers no registration', async () => {
    serve({ [LINK]: fail(403, 'LINK_UNAVAILABLE', 'server wording that must not be shown') })
    const { user } = open()

    await submitDetails(user)

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent(/^Please contact the hospital\.$/)
    expect(screen.queryByRole('button', { name: 'Register as a new patient' })).not.toBeInTheDocument()
  })

  it('CONFLICT — says the account is already linked at this hospital and offers no registration', async () => {
    serve({ [LINK]: conflict(ALREADY_LINKED) })
    const { user } = open()

    await submitDetails(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Your account is already linked to a record at this hospital.',
    )
    expect(screen.queryByRole('button', { name: 'Register as a new patient' })).not.toBeInTheDocument()
  })

  it.each([
    ['the consent version is stale', conflict(STALE_CONSENT)],
    ['the conflict is one this build does not know', conflict('Something new.')],
  ])('STALE — asks for a reload and never claims a record is linked when %s', async (_case, outcome) => {
    serve({ [LINK]: outcome })
    const { user } = open()

    await submitDetails(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(RELOAD)
    expect(document.body).not.toHaveTextContent(/already linked/i)
    expect(document.body).not.toHaveTextContent('The policy has changed')
    expect(screen.queryByRole('button', { name: 'Register as a new patient' })).not.toBeInTheDocument()
  })

  it('CONSENT — each tick says only what is agreed to, and no terms or privacy acceptance is sent', async () => {
    const api = serve({ [LINK]: notFound(), [REGISTER]: ok({}, 201), [ME]: ok(me([cityCare])) })
    const { user } = open()

    await codeField()
    expect(screen.getByRole('checkbox', { name: 'I agree to connect my app account to my record at this hospital.' })).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/terms|privacy|polic/i)

    await submitDetails(user)
    await user.click(await screen.findByRole('button', { name: 'Register as a new patient' }))
    await screen.findByRole('heading', { name: 'Register as a new patient' })
    expect(
      screen.getByRole('checkbox', {
        name: 'I agree to give this hospital my name, date of birth, gender and mobile number to register me as a patient, and to connect my app account to the new record.',
      }),
    ).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent(/terms|privacy|polic/i)
    await fillRegistration(user)
    await screen.findByText('You are registered')

    // The only consent on the wire is the version of the hospital text shown.
    expect(Object.keys(bodyOf(api.calls(LINK)[0]) as object).sort()).toEqual(['consent_policy_version', 'date_of_birth'])
    expect(Object.keys(bodyOf(api.calls(REGISTER)[0]) as object).sort()).toEqual([
      'consent_policy_version',
      'date_of_birth',
      'first_name',
      'gender',
      'last_name',
    ])
    expect(api.sent.map((request) => request.url)).toEqual(
      expect.not.arrayContaining([expect.stringMatching(/consent|polic|terms|privacy/)]),
    )
  })

  it.each([
    ['a validation error (422)', fail(422, 'VALIDATION_ERROR'), /Some of these details are not valid/],
    ['a server error (500)', fail(500, 'INTERNAL_ERROR', 'Traceback…'), /Something went wrong/],
    ['a pending platform policy (403)', fail(403, 'CONSENT_REQUIRED', 'server wording'), /^We cannot complete this in the app right now\. Please try again later\.$/],
    ['the rate limit (429)', fail(429, 'RATE_LIMITED'), /^Too many attempts\. Please wait a few minutes and try again\.$/],
    ['an unknown refusal (403)', fail(403, 'FORBIDDEN'), /Something went wrong/],
  ])('shows a safe message for %s and lets the patient try again', async (_case, outcome, message) => {
    serve({ [LINK]: outcome })
    const { user } = open()

    await submitDetails(user)

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(document.body).not.toHaveTextContent('Traceback')
    await waitFor(() => expect(findMyRecord()).toBeEnabled())
  })
})
