import { AxiosError, type AxiosAdapter } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

/**
 * Two tabs of one browser share ONE refresh cookie. The server rotates it on
 * every use and treats a second use of the spent one as theft, revoking every
 * session of the account. These tests put two independent copies of the app's
 * client (two "tabs": separate module instances, separate in-memory stores)
 * in front of one cookie jar and one such server.
 */

const LOCK_NAME = 'atheris-patient-refresh'

/** An exclusive-only stand-in for `navigator.locks`, shared by every tab. */
function fakeLocks() {
  let tail: Promise<unknown> = Promise.resolve()
  const names: string[] = []
  const request = (name: string, options: { signal?: AbortSignal }, callback: () => Promise<unknown>) => {
    names.push(name)
    const previous = tail
    let aborted = false
    const run = new Promise((resolve, reject) => {
      options.signal?.addEventListener('abort', () => {
        aborted = true
        reject(new DOMException('The request was aborted.', 'AbortError'))
      })
      void previous.then(() => (aborted ? undefined : callback().then(resolve, reject)))
    })
    // A waiter that gave up never held the lock, so it does not delay the next.
    tail = run.catch(() => undefined)
    return run
  }
  return { request, names }
}

/** The browser's cookie jar and the server behind it, as both tabs see them. */
function browserAndServer() {
  const state = {
    jar: 'refresh-1' as string | null,
    valid: 'refresh-1' as string | null,
    issued: 1,
    inFlight: 0,
    mostAtOnce: 0,
    refreshCalls: 0,
    reuseDetected: false,
    /** What the next refresh answers instead of rotating, once. */
    nextStatus: null as number | null,
    /** Resolve to let the refresh in flight finish. */
    release: [] as (() => void)[],
    held: false,
  }

  const adapter: AxiosAdapter = async (config) => {
    if (`${(config.method ?? '').toUpperCase()} ${config.url}` !== 'POST /auth/refresh') {
      throw new AxiosError('unexpected request', 'ERR_NETWORK', config)
    }
    // The browser attaches whatever the jar holds when the request LEAVES.
    const presented = state.jar
    state.refreshCalls += 1
    state.inFlight += 1
    state.mostAtOnce = Math.max(state.mostAtOnce, state.inFlight)
    if (state.held) await new Promise<void>((resolve) => state.release.push(resolve))
    else await new Promise((resolve) => setTimeout(resolve, 5))
    state.inFlight -= 1

    const refuse = (status: number) => {
      const response = { status, statusText: '', headers: {}, config, data: { success: false, error_code: 'X' } }
      throw new AxiosError('Request failed', 'ERR_BAD_REQUEST', config, null, response)
    }
    if (state.nextStatus !== null) {
      const status = state.nextStatus
      state.nextStatus = null
      if (status === 401) state.jar = state.valid = null
      refuse(status)
    }
    if (presented === null || presented !== state.valid) {
      // A spent or unknown token: reuse detection revokes everything.
      state.reuseDetected = state.reuseDetected || presented !== null
      state.jar = state.valid = null
      refuse(401)
    }
    state.issued += 1
    state.jar = state.valid = `refresh-${state.issued}`
    return {
      status: 200,
      statusText: '',
      headers: {},
      config,
      data: { success: true, data: { access_token: `access-${state.issued}`, expires_in: 900 } },
    }
  }
  return { state, adapter }
}

/** A new tab: its own copy of the client module and of the auth store. */
async function openTab(adapter: AxiosAdapter, accessToken: string | null = null) {
  vi.resetModules()
  const client = await import('@/api/client')
  const { usePatientAuthStore } = await import('@/store/patient-auth-store')
  client.api.defaults.adapter = adapter
  usePatientAuthStore.setState({ accessToken, isRestoring: true, signOutReason: null })
  return { refreshSession: client.refreshSession, session: () => usePatientAuthStore.getState() }
}

const stubLocks = (locks: unknown) => vi.stubGlobal('navigator', { ...globalThis.navigator, locks })

describe('refresh across tabs', () => {
  let locks: ReturnType<typeof fakeLocks>

  beforeEach(() => {
    locks = fakeLocks()
    stubLocks(locks)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('ATTACK SURFACE — without the lock, two tabs restoring together present the same cookie and trip reuse detection', async () => {
    stubLocks(undefined)
    const { state, adapter } = browserAndServer()
    const [a, b] = [await openTab(adapter), await openTab(adapter)]

    const outcomes = await Promise.all([a.refreshSession(), b.refreshSession()])

    // This is the failure the lock exists to prevent, shown on the same model.
    expect(state.mostAtOnce).toBe(2)
    expect(state.reuseDetected).toBe(true)
    expect(outcomes.filter((outcome) => outcome.sessionEnded)).toHaveLength(1)
  })

  it('two tabs restoring together refresh strictly one after the other, each with the rotated cookie', async () => {
    const { state, adapter } = browserAndServer()
    const [a, b] = [await openTab(adapter), await openTab(adapter)]

    const [first, second] = await Promise.all([a.refreshSession(), b.refreshSession()])

    expect(state.refreshCalls).toBe(2)
    expect(state.mostAtOnce).toBe(1)
    expect(state.reuseDetected).toBe(false)
    expect(locks.names).toEqual([LOCK_NAME, LOCK_NAME])
    // Each tab holds its own token, from its own request; nothing was shared.
    expect(first).toEqual({ token: 'access-2', sessionEnded: false })
    expect(second).toEqual({ token: 'access-3', sessionEnded: false })
    expect(a.session().accessToken).toBe('access-2')
    expect(b.session().accessToken).toBe('access-3')
  })

  it('the second tab does not send its request until the first has its answer', async () => {
    const { state, adapter } = browserAndServer()
    state.held = true
    const [a, b] = [await openTab(adapter), await openTab(adapter)]

    const both = Promise.all([a.refreshSession(), b.refreshSession()])
    await expect.poll(() => state.refreshCalls).toBe(1)
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(state.refreshCalls).toBe(1)

    state.held = false
    state.release.shift()?.()
    await both
    expect(state.refreshCalls).toBe(2)
    expect(state.mostAtOnce).toBe(1)
  })

  it('callers inside one tab still share one request under the lock', async () => {
    const { state, adapter } = browserAndServer()
    const a = await openTab(adapter)

    const [first, second] = await Promise.all([a.refreshSession(), a.refreshSession()])

    expect(second).toBe(first)
    expect(state.refreshCalls).toBe(1)
  })

  it('a refresh that could not be reached in one tab leaves the other tab to restore normally', async () => {
    const { state, adapter } = browserAndServer()
    state.nextStatus = 503
    const [a, b] = [await openTab(adapter, 'access-old'), await openTab(adapter)]

    const [first, second] = await Promise.all([a.refreshSession(), b.refreshSession()])

    // Tab A learned nothing about its session and keeps it; the lock was released.
    expect(first).toEqual({ token: null, sessionEnded: false })
    expect(a.session().accessToken).toBe('access-old')
    expect(second).toEqual({ token: 'access-2', sessionEnded: false })
    expect(b.session().accessToken).toBe('access-2')
    expect(state.mostAtOnce).toBe(1)
  })

  it('a tab is signed out only by the refusal of ITS OWN refresh, never by another tab’s', async () => {
    const { state, adapter } = browserAndServer()
    const [a, b] = [await openTab(adapter, 'access-a'), await openTab(adapter, 'access-b')]

    // Tab A refreshes and is fine. Nothing tells tab B anything.
    expect((await a.refreshSession()).token).toBe('access-2')
    expect(b.session().accessToken).toBe('access-b')

    // The server now refuses tab A's next refresh (and ends the session).
    state.nextStatus = 401
    expect(await a.refreshSession()).toEqual({ token: null, sessionEnded: true })
    expect(a.session().accessToken).toBeNull()
    expect(a.session().signOutReason).toBe('session_ended')
    // Tab B is untouched until it asks for itself …
    expect(b.session().accessToken).toBe('access-b')
    expect(b.session().signOutReason).toBeNull()

    // … and then it is signed out because its own refresh is rejected.
    expect(await b.refreshSession()).toEqual({ token: null, sessionEnded: true })
    expect(b.session().accessToken).toBeNull()
    expect(b.session().signOutReason).toBe('session_ended')
    expect(state.mostAtOnce).toBe(1)
  })

  it('a tab that waits too long gives up WITHOUT refreshing alongside the holder, and keeps its session', async () => {
    const { state, adapter } = browserAndServer()
    state.held = true
    const [a, b] = [await openTab(adapter, 'access-a'), await openTab(adapter, 'access-b')]
    const { REFRESH_LOCK_WAIT_MS } = await import('@/api/client')
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })

    const stuck = a.refreshSession()
    await vi.advanceTimersByTimeAsync(0)
    expect(state.refreshCalls).toBe(1)
    const waiting = b.refreshSession()
    await vi.advanceTimersByTimeAsync(REFRESH_LOCK_WAIT_MS + 1)

    expect(await waiting).toEqual({ token: null, sessionEnded: false })
    expect(state.refreshCalls).toBe(1)
    expect(b.session().accessToken).toBe('access-b')
    expect(b.session().signOutReason).toBeNull()

    state.release.shift()?.()
    expect((await stuck).token).toBe('access-2')
  })

  it('FALLBACK — without Web Locks a tab still keeps its own callers to one request', async () => {
    stubLocks(undefined)
    const { state, adapter } = browserAndServer()
    const a = await openTab(adapter)

    const [first, second] = await Promise.all([a.refreshSession(), a.refreshSession()])

    expect(second).toBe(first)
    expect(first).toEqual({ token: 'access-2', sessionEnded: false })
    expect(state.refreshCalls).toBe(1)
  })

  it('FALLBACK — a browser that refuses the lock is treated as one without locks', async () => {
    stubLocks({ request: () => Promise.reject(new DOMException('no', 'SecurityError')) })
    const { state, adapter } = browserAndServer()
    const a = await openTab(adapter)

    expect(await a.refreshSession()).toEqual({ token: 'access-2', sessionEnded: false })
    expect(state.refreshCalls).toBe(1)
  })
})
