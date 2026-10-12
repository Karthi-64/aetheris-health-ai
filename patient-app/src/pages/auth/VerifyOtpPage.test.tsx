import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { useOtpChallengeStore } from '@/store/otp-challenge-store'
import { usePatientAuthStore } from '@/store/patient-auth-store'
import { bodyOf, fail, headerOf, ok, serve } from '@/test/fakeApi'
import { me, otpRequested, PHONE_E164, PHONE_MASKED, verifiedSession } from '@/test/fixtures'
import { isSignedIn, renderApp } from '@/test/renderApp'

const VERIFY = 'POST /auth/otp/verify'
const REQUEST = 'POST /auth/otp/request'
const ME = 'GET /me'

/** As the login page leaves things: a challenge in memory, resend not yet allowed. */
function pendingChallenge(id = 'challenge-1', resendInSeconds = 60) {
  useOtpChallengeStore.setState({
    challenge: { id, phone: PHONE_E164, resendAt: Date.now() + resendInSeconds * 1000 },
  })
}

const codeField = () => screen.findByLabelText('6-digit code')
const submit = () => screen.getByRole('button', { name: 'Verify and continue' })

describe('verify — code entry', () => {
  it('sends a visitor without a pending challenge back to the phone page', async () => {
    serve({})
    const { router } = renderApp('/verify-otp')

    expect(await screen.findByRole('heading', { name: 'Sign in with your mobile number' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
  })

  it('is a numeric one-time-code field a phone can fill from the text message', async () => {
    pendingChallenge()
    serve({})
    renderApp('/verify-otp')

    const field = await codeField()
    expect(field).toHaveAttribute('autocomplete', 'one-time-code')
    expect(field).toHaveAttribute('inputmode', 'numeric')
    expect(field).toHaveAttribute('maxlength', '6')
  })

  it.each(['', '12345', '12a456'])('refuses "%s" without calling the server', async (typed) => {
    pendingChallenge()
    const api = serve({})
    const { user } = renderApp('/verify-otp')

    const field = await codeField()
    if (typed) await user.type(field, typed)
    await user.click(submit())

    expect(await screen.findByText('Enter the 6 digits from the text message.')).toBeInTheDocument()
    expect(field).toHaveAttribute('aria-invalid', 'true')
    expect(api.sent).toHaveLength(0)
  })

  it('signs the patient in on a correct code and shows the home page', async () => {
    pendingChallenge('challenge-9')
    const api = serve({ [VERIFY]: ok(verifiedSession('access-new')), [ME]: ok(me()) })
    const { user, router } = renderApp('/verify-otp')

    await user.type(await codeField(), '482913')
    await user.click(submit())

    expect(await screen.findByText(PHONE_MASKED)).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/')

    const [request] = api.calls(VERIFY)
    expect(bodyOf(request)).toEqual({ challenge_id: 'challenge-9', code: '482913' })
    expect(headerOf(request, 'Authorization')).toBeUndefined()

    // The token is in memory and is what the next call carries.
    expect(usePatientAuthStore.getState().accessToken).toBe('access-new')
    expect(headerOf(api.calls(ME)[0], 'Authorization')).toBe('Bearer access-new')
    // The spent challenge is forgotten, and neither the code nor a token is on screen.
    expect(useOtpChallengeStore.getState().challenge).toBeNull()
    expect(document.body).not.toHaveTextContent('482913')
    expect(document.body).not.toHaveTextContent('access-new')
  })

  it('answers every rejected code with the same message and stays signed out', async () => {
    pendingChallenge()
    const api = serve({ [VERIFY]: fail(401, 'OTP_INVALID', 'server wording that must not be shown') })
    const { user, router } = renderApp('/verify-otp')

    await user.type(await codeField(), '000000')
    await user.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'That code did not work. Check it and try again, or ask for a new code.',
    )
    expect(document.body).not.toHaveTextContent('server wording')
    expect(router.state.location.pathname).toBe('/verify-otp')
    expect(isSignedIn()).toBe(false)
    // A 401 from the code check is an answer, not an expired session: no refresh.
    expect(api.calls('POST /auth/refresh')).toHaveLength(0)
    await waitFor(() => expect(submit()).toBeEnabled())
  })

  it('shows the limit message when code checks are throttled', async () => {
    pendingChallenge()
    serve({ [VERIFY]: fail(429, 'OTP_THROTTLED') })
    const { user } = renderApp('/verify-otp')

    await user.type(await codeField(), '123456')
    await user.click(submit())

    expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts.')
    expect(isSignedIn()).toBe(false)
  })

  describe('asking for a new code', () => {
    function withFakeClock() {
      vi.useFakeTimers({ shouldAdvanceTime: true })
      return userEvent.setup({ advanceTimers: vi.advanceTimersByTime })
    }
    const tick = (seconds: number) => act(() => vi.advanceTimersByTime(seconds * 1000))

    it('is locked for the countdown the server set, then allowed', async () => {
      withFakeClock()
      pendingChallenge('challenge-1', 60)
      const api = serve({})
      renderApp('/verify-otp')

      const resend = await screen.findByRole('button', { name: 'Send a new code in 60s' })
      expect(resend).toBeDisabled()

      tick(25)
      expect(screen.getByRole('button', { name: 'Send a new code in 35s' })).toBeDisabled()

      tick(34)
      expect(screen.getByRole('button', { name: 'Send a new code in 1s' })).toBeDisabled()

      tick(1)
      expect(screen.getByRole('button', { name: 'Send a new code' })).toBeEnabled()
      expect(api.sent).toHaveLength(0)
    })

    it('requests a code for the same number, uses the new challenge and restarts the countdown', async () => {
      const user = withFakeClock()
      pendingChallenge('challenge-old', 60)
      const api = serve({
        [REQUEST]: ok(otpRequested('challenge-new'), 202),
        [VERIFY]: ok(verifiedSession()),
        [ME]: ok(me()),
      })
      renderApp('/verify-otp')
      await codeField()

      tick(60)
      await user.click(screen.getByRole('button', { name: 'Send a new code' }))

      expect(await screen.findByText('A new code is on its way.')).toBeInTheDocument()
      expect(bodyOf(api.calls(REQUEST)[0])).toEqual({ phone: PHONE_E164 })
      expect(screen.getByRole('button', { name: 'Send a new code in 60s' })).toBeDisabled()

      await user.type(await codeField(), '654321')
      await user.click(submit())

      await waitFor(() => expect(api.calls(VERIFY)).toHaveLength(1))
      expect(bodyOf(api.calls(VERIFY)[0])).toEqual({ challenge_id: 'challenge-new', code: '654321' })
    })

    it('shows the limit message when the resend is throttled and keeps the current challenge', async () => {
      const user = withFakeClock()
      pendingChallenge('challenge-old', 60)
      serve({ [REQUEST]: fail(429, 'OTP_THROTTLED') })
      renderApp('/verify-otp')
      await codeField()

      tick(60)
      await user.click(screen.getByRole('button', { name: 'Send a new code' }))

      expect(await screen.findByRole('alert')).toHaveTextContent('Too many attempts.')
      expect(useOtpChallengeStore.getState().challenge?.id).toBe('challenge-old')
    })
  })
})
