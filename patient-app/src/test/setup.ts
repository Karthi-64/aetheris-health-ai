import '@testing-library/jest-dom/vitest'
import { cleanup } from '@testing-library/react'
import { afterEach, beforeEach, expect, vi } from 'vitest'
import { queryClient } from '@/lib/query-client'
import { useOtpChallengeStore } from '@/store/otp-challenge-store'
import { usePatientAuthStore } from '@/store/patient-auth-store'
import { uninstallFakeApi } from '@/test/fakeApi'

// A failed query is retried once after a second in the app; tests want the
// first answer shown at once.
queryClient.setDefaultOptions({
  ...queryClient.getDefaultOptions(),
  queries: { ...queryClient.getDefaultOptions().queries, retry: false },
})

// The lint-rule tests run in Node, where there is no DOM and no web storage.
const hasDom = typeof window !== 'undefined'

let storageWrite: ReturnType<typeof vi.spyOn> | null = null

beforeEach(() => {
  if (hasDom) storageWrite = vi.spyOn(Storage.prototype, 'setItem')
  // Most tests start after session restore has answered "no session".
  usePatientAuthStore.setState({ accessToken: null, isRestoring: false, signOutReason: null })
  useOtpChallengeStore.setState({ challenge: null })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  queryClient.clear()

  // Every test doubles as a storage test: whatever it did, the app must have
  // left web storage untouched (the access token is memory-only).
  if (hasDom) {
    expect(storageWrite).not.toHaveBeenCalled()
    expect(window.localStorage).toHaveLength(0)
    expect(window.sessionStorage).toHaveLength(0)
  }
  vi.restoreAllMocks()

  // A request no fake route answered is a contract the test did not expect.
  expect(uninstallFakeApi()).toEqual([])
})
