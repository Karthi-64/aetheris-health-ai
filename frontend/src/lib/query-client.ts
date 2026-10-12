import { QueryClient } from '@tanstack/react-query'
import { ApiError } from '@/api/types'

/**
 * Retry a failed query once, and only when trying again could help: the network
 * dropped or the server failed (5xx). A 4xx is the server's answer — not found,
 * not allowed — and asking again only delays showing it.
 */
function retryQuery(failureCount: number, error: unknown): boolean {
  if (failureCount >= 1) return false
  const status = error instanceof ApiError ? error.status : undefined
  return !(status !== undefined && status >= 400 && status < 500)
}

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      retry: retryQuery,
      refetchOnWindowFocus: false,
    },
  },
})
