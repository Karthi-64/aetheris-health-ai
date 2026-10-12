import { describe, it, expect, afterEach, vi } from 'vitest'
import { QueryObserver } from '@tanstack/react-query'
import { ApiError } from '@/api/types'
import { queryClient } from './query-client'

/** Run a query that always fails with `error` to its final state; return how often it was tried. */
async function attemptsFor(error: unknown): Promise<number> {
  const queryFn = vi.fn(async () => {
    throw error
  })
  const observer = new QueryObserver(queryClient, { queryKey: ['probe'], queryFn, retryDelay: 0 })
  const unsubscribe = observer.subscribe(() => {})
  await vi.waitFor(() => expect(observer.getCurrentResult().isError).toBe(true))
  unsubscribe()
  return queryFn.mock.calls.length
}

describe('query client retry policy', () => {
  afterEach(() => queryClient.clear())

  it.each([400, 401, 403, 404, 409, 422])('does not retry a %i: it is the answer', async (status) => {
    expect(await attemptsFor(new ApiError('No.', 'refused', status))).toBe(1)
  })

  it('retries a server failure once', async () => {
    expect(await attemptsFor(new ApiError('Boom.', 'internal_error', 500))).toBe(2)
  })

  it('retries a network failure once', async () => {
    expect(await attemptsFor(new ApiError('Network Error', 'network_error'))).toBe(2)
  })
})
