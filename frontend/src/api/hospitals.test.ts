import { createElement, type ReactNode } from 'react'
import { describe, it, expect, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fail, installFakeApi, ok, type FakeApi, type Handler } from '@/test/fakeApi'
import { AI_SLOT_RECOMMENDATION_FLAG, isFeatureAvailable, useFeatureFlags } from './hospitals'

/**
 * `GET /hospitals/current/feature-flags` tells the browser which gated features
 * it may offer. The real hook and Axios run; only the adapter is replaced.
 */
describe('useFeatureFlags', () => {
  let fake: FakeApi

  afterEach(() => {
    fake.restore()
  })

  function renderFlags(handler: Handler, enabled?: boolean) {
    fake = installFakeApi(handler)
    const client = new QueryClient()
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children)
    return renderHook(() => useFeatureFlags(enabled), { wrapper })
  }

  it('reads the flags of the current hospital', async () => {
    const flags = { flags: { [AI_SLOT_RECOMMENDATION_FLAG]: { available: true } } }
    const { result } = renderFlags(() => ok(flags))

    await waitFor(() => expect(result.current.isSuccess).toBe(true))

    expect(fake.sent).toHaveLength(1)
    const [request] = fake.sent
    expect(request.method).toBe('get')
    expect(`${request.baseURL}${request.url}`).toBe('/api/v1/hospitals/current/feature-flags')
    expect(request.params).toBeUndefined()
    expect(request.data).toBeUndefined()
    expect(result.current.data).toEqual(flags)
    expect(isFeatureAvailable(result.current.data, AI_SLOT_RECOMMENDATION_FLAG)).toBe(true)
  })

  it('sends nothing when the caller says the user has no use for it', async () => {
    const { result } = renderFlags(() => ok({ flags: {} }), false)

    await new Promise((resolve) => setTimeout(resolve, 20))

    expect(fake.sent).toHaveLength(0)
    expect(result.current.data).toBeUndefined()
  })

  it('does not retry a failed read', async () => {
    const { result } = renderFlags(() => fail(500, 'Internal error.'))

    await waitFor(() => expect(result.current.isError).toBe(true))

    expect(fake.sent).toHaveLength(1)
    expect(isFeatureAvailable(result.current.data, AI_SLOT_RECOMMENDATION_FLAG)).toBe(false)
  })
})

describe('isFeatureAvailable', () => {
  const KEY = AI_SLOT_RECOMMENDATION_FLAG

  it('is true only for an explicit available: true under the asked key', () => {
    expect(isFeatureAvailable({ flags: { [KEY]: { available: true } } }, KEY)).toBe(true)
    expect(isFeatureAvailable({ flags: { 'feature.other': { available: true } } }, KEY)).toBe(false)
  })

  it.each<[string, unknown]>([
    ['nothing loaded', undefined],
    ['null', null],
    ['an empty list', []],
    ['no flags', {}],
    ['flags without the key', { flags: {} }],
    ['flags that are null', { flags: null }],
    ['available false', { flags: { [KEY]: { available: false } } }],
    ['a truthy value that is not true', { flags: { [KEY]: { available: 'true' } } }],
    ['a bare boolean', { flags: { [KEY]: true } }],
    ['a null state', { flags: { [KEY]: null } }],
  ])('fails closed for %s', (_name, value) => {
    expect(isFeatureAvailable(value, KEY)).toBe(false)
  })
})
