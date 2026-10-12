import { screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useOtpChallengeStore } from '@/store/otp-challenge-store'
import { bodyOf, fail, headerOf, ok, serve } from '@/test/fakeApi'
import { otpRequested, PHONE_E164, PHONE_TYPED } from '@/test/fixtures'
import { renderApp } from '@/test/renderApp'

const REQUEST = 'POST /auth/otp/request'

const phoneField = () => screen.findByLabelText('Mobile number')
const submit = () => screen.getByRole('button', { name: 'Send code' })

describe('login — phone entry', () => {
  it('is a labelled telephone field a phone can autofill', async () => {
    serve({})
    renderApp('/login')

    const field = await phoneField()
    expect(field).toHaveAttribute('type', 'tel')
    expect(field).toHaveAttribute('autocomplete', 'tel')
    expect(field).toHaveAttribute('inputmode', 'tel')
    expect(field).toHaveAccessibleDescription(/country code/)
  })

  it('asks for a number before sending anything', async () => {
    const api = serve({})
    const { user } = renderApp('/login')
    await phoneField()

    await user.click(submit())

    const field = await phoneField()
    expect(await screen.findByText('Enter your mobile number.')).toHaveAttribute('role', 'alert')
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(field).toHaveAccessibleDescription(/Enter your mobile number\./)
    expect(api.sent).toHaveLength(0)
  })

  it.each(['98765 43210', '+91 12', 'not a number', '+0 9876543210'])(
    'refuses "%s" without guessing a country code',
    async (typed) => {
      const api = serve({})
      const { user } = renderApp('/login')

      await user.type(await phoneField(), typed)
      await user.click(submit())

      expect(await screen.findByText(/starting with the country code/)).toBeInTheDocument()
      expect(api.sent).toHaveLength(0)
    },
  )

  it('sends the number as E.164 and moves to the code page with the challenge kept out of the URL', async () => {
    const api = serve({ [REQUEST]: ok(otpRequested('challenge-abc'), 202) })
    const { user, router } = renderApp('/login')

    await user.type(await phoneField(), PHONE_TYPED)
    await user.click(submit())

    expect(await screen.findByRole('heading', { name: 'Enter the 6-digit code' })).toBeInTheDocument()

    const [request] = api.calls(REQUEST)
    expect(api.sent).toHaveLength(1)
    expect(bodyOf(request)).toEqual({ phone: PHONE_E164 })
    // A public endpoint: no bearer token, and no CSRF header to imitate one.
    expect(headerOf(request, 'Authorization')).toBeUndefined()

    const { pathname, search, hash, state } = router.state.location
    expect(pathname).toBe('/verify-otp')
    expect(search).toBe('')
    expect(hash).toBe('')
    expect(state).toBeNull()
    expect(document.body).not.toHaveTextContent('challenge-abc')
    expect(useOtpChallengeStore.getState().challenge?.id).toBe('challenge-abc')
  })

  it('shows one message when a limit is reached, whichever limit it was', async () => {
    serve({ [REQUEST]: fail(429, 'OTP_THROTTLED', 'server wording that must not be shown', { 'retry-after': '600' }) })
    const { user, router } = renderApp('/login')

    await user.type(await phoneField(), PHONE_TYPED)
    await user.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Too many attempts. Please wait a few minutes and try again.',
    )
    expect(document.body).not.toHaveTextContent('server wording')
    expect(router.state.location.pathname).toBe('/login')
    expect(useOtpChallengeStore.getState().challenge).toBeNull()
  })

  it('says codes cannot be sent when the service is unavailable', async () => {
    serve({ [REQUEST]: fail(503, 'SERVICE_UNAVAILABLE') })
    const { user, router } = renderApp('/login')

    await user.type(await phoneField(), PHONE_TYPED)
    await user.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'We cannot send codes right now. Please try again later.',
    )
    expect(router.state.location.pathname).toBe('/login')
    // The form is usable again for another try.
    await waitFor(() => expect(submit()).toBeEnabled())
  })

  it('puts a number the server will not accept (422) on the field, in the app’s own words', async () => {
    serve({ [REQUEST]: fail(422, 'VALIDATION_ERROR', 'Value error, +4412345678 is not allowed') })
    const { user } = renderApp('/login')

    await user.type(await phoneField(), '+44 1234 5678')
    await user.click(submit())

    expect(await screen.findByText(/We cannot send a code to this number/)).toBeInTheDocument()
    expect(await phoneField()).toHaveAttribute('aria-invalid', 'true')
    expect(document.body).not.toHaveTextContent('is not allowed')
  })

  it('shows a generic message when the network fails', async () => {
    serve({ [REQUEST]: fail(500, 'INTERNAL_ERROR', 'Traceback…') })
    const { user } = renderApp('/login')

    await user.type(await phoneField(), PHONE_TYPED)
    await user.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong.')
    expect(document.body).not.toHaveTextContent('Traceback')
  })
})
