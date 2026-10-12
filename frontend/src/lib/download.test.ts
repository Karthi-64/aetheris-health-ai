import { describe, it, expect } from 'vitest'
import { AxiosError, type InternalAxiosRequestConfig } from 'axios'
import { ApiError } from '@/api/types'
import { blobApiError, filenameFromContentDisposition } from './download'

describe('filenameFromContentDisposition', () => {
  it('reads the quoted filename the API sends', () => {
    expect(filenameFromContentDisposition('attachment; filename="audit-logs-20261005-101500.csv"')).toBe(
      'audit-logs-20261005-101500.csv',
    )
  })

  it('reads an unquoted filename', () => {
    expect(filenameFromContentDisposition('attachment; filename=export.json')).toBe('export.json')
  })

  it('prefers the RFC 5987 form and decodes it', () => {
    expect(
      filenameFromContentDisposition("attachment; filename=\"fallback.csv\"; filename*=UTF-8''r%C3%A9sum%C3%A9.csv"),
    ).toBe('résumé.csv')
  })

  it('drops any directory part', () => {
    expect(filenameFromContentDisposition('attachment; filename="../../etc/passwd"')).toBe('passwd')
    expect(filenameFromContentDisposition('attachment; filename="C:\\\\temp\\\\a.csv"')).toBe('a.csv')
  })

  it('is undefined when no usable name is given', () => {
    expect(filenameFromContentDisposition('attachment')).toBeUndefined()
    expect(filenameFromContentDisposition('attachment; filename=""')).toBeUndefined()
    expect(filenameFromContentDisposition(undefined)).toBeUndefined()
    expect(filenameFromContentDisposition(42)).toBeUndefined()
  })
})

describe('blobApiError', () => {
  /** The error Axios raises for a non-2xx answer to a `responseType: 'blob'` request. */
  const refused = (status: number, data: unknown) => {
    const config = { url: '/reports/revenue/export' } as InternalAxiosRequestConfig
    return new AxiosError('Request failed with status code ' + status, 'ERR_BAD_REQUEST', config, null, {
      status,
      statusText: '',
      headers: {},
      config,
      data,
    })
  }
  const envelope = (body: Record<string, unknown>) =>
    new Blob([JSON.stringify({ success: false, ...body })], { type: 'application/json' })

  it('reads the message, code and status out of a blob body', async () => {
    const err = await blobApiError(
      refused(403, envelope({ message: 'Permission denied. Required: report.export.', error_code: 'PERMISSION_DENIED' })),
    )
    expect(err).toBeInstanceOf(ApiError)
    expect(err.message).toBe('Permission denied. Required: report.export.')
    expect(err.code).toBe('PERMISSION_DENIED')
    expect(err.status).toBe(403)
  })

  it('keeps the field errors', async () => {
    const errors = { errors: [{ field: 'format', message: 'PDF export is not available yet. Use format=csv.' }] }
    const err = await blobApiError(
      refused(422, envelope({ message: 'PDF export is not available yet. Use format=csv.', error_code: 'VALIDATION_ERROR', errors })),
    )
    expect(err.status).toBe(422)
    expect(err.details).toEqual(errors)
  })

  it('reads a body that already arrived as JSON', async () => {
    const err = await blobApiError(refused(404, { message: 'Unknown report: `bogus`.', error_code: 'RESOURCE_NOT_FOUND' }))
    expect(err.message).toBe('Unknown report: `bogus`.')
    expect(err.status).toBe(404)
  })

  it('falls back to the transport error when the blob is not JSON', async () => {
    const err = await blobApiError(refused(502, new Blob(['<html>Bad Gateway</html>'], { type: 'text/html' })))
    expect(err.message).toBe('Request failed with status code 502')
    expect(err.code).toBe('network_error')
    expect(err.status).toBe(502)
  })

  it('handles a request that got no answer, and a value that is not an Axios error', async () => {
    const offline = await blobApiError(new AxiosError('Network Error', 'ERR_NETWORK'))
    expect(offline.message).toBe('Network Error')
    expect(offline.code).toBe('network_error')
    expect(offline.status).toBeUndefined()

    expect((await blobApiError(new Error('boom'))).message).toBe('boom')
    expect((await blobApiError('nope')).message).toBe('Unexpected error')
  })
})
