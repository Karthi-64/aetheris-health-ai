import { ApiError } from '@atheris/api-core'
import { authStrings } from '@/pages/auth/strings'

/**
 * The message for a failed code request or code check. The app's own wording
 * is used throughout: the server deliberately says the same thing for every
 * cause, and nothing it sends is echoed to the screen.
 */
export function otpErrorMessage(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.code === 'OTP_INVALID' || err.status === 401) return authStrings.codeRejected
    if (err.code === 'OTP_THROTTLED' || err.status === 429) return authStrings.throttled
    if (err.code === 'SERVICE_UNAVAILABLE' || err.status === 503) return authStrings.unavailable
  }
  return authStrings.failed
}
