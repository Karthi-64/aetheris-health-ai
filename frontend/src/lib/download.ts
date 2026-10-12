import { AxiosError } from 'axios'
import { ApiError } from '@/api/types'

/**
 * The filename a `Content-Disposition` header names, or undefined if it names
 * none. Reads the plain `filename="…"` form and the RFC 5987 `filename*=`
 * form, preferring the latter as the header's own rules do. Any directory part
 * is dropped: the name is only ever used as a suggestion for a local save.
 */
export function filenameFromContentDisposition(header: unknown): string | undefined {
  if (typeof header !== 'string') return undefined
  let name: string | undefined
  const extended = /filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)/.exec(header)
  if (extended) {
    try {
      name = decodeURIComponent(extended[1].trim())
    } catch {
      name = undefined
    }
  }
  if (!name) {
    const plain = /filename\s*=\s*(?:"([^"]*)"|([^;]+))/.exec(header)
    name = (plain?.[1] ?? plain?.[2])?.trim()
  }
  const base = name?.split(/[\\/]/).pop()
  return base || undefined
}

/** Hand a file the app already holds to the browser's download manager. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.appendChild(link)
  link.click()
  link.remove()
  URL.revokeObjectURL(url)
}

/**
 * The typed error for a failed file request.
 *
 * A download that fails is still answered with the API's JSON envelope, but
 * the request asked for a blob, so that is what the envelope arrives in. This
 * reads it back out so the caller sees the server's message, code, status and
 * field errors exactly as it would for any other call.
 */
export async function blobApiError(err: unknown): Promise<ApiError> {
  if (!(err instanceof AxiosError)) {
    return new ApiError(err instanceof Error ? err.message : 'Unexpected error')
  }
  let body: unknown = err.response?.data
  if (body instanceof Blob) {
    try {
      body = JSON.parse(await body.text())
    } catch {
      body = undefined
    }
  }
  const envelope = (body ?? {}) as { message?: string; error_code?: string; errors?: unknown }
  return new ApiError(
    envelope.message ?? err.message,
    envelope.error_code ?? 'network_error',
    err.response?.status,
    envelope.errors,
  )
}
