import { describe, expect, it } from 'vitest'
import { safeImageUrl } from '@/lib/safeUrl'

/**
 * ATTACK: a hospital's logo address is whatever was typed into its settings.
 * A hostile one could try to run script, read this origin with the patient's
 * session, or have the browser choose the scheme.
 * OUTCOME: only an absolute https:// address ever reaches `<img src>`.
 */
describe('safeImageUrl', () => {
  it('accepts an absolute https address', () => {
    expect(safeImageUrl('https://cdn.example.test/logos/a.png', false)).toBe('https://cdn.example.test/logos/a.png')
    expect(safeImageUrl('https://cdn.example.test/a.png?v=2#x', false)).toBe('https://cdn.example.test/a.png?v=2#x')
  })

  it('accepts http only in a development build', () => {
    expect(safeImageUrl('http://localhost:8000/static/a.png', true)).toBe('http://localhost:8000/static/a.png')
    expect(safeImageUrl('http://localhost:8000/static/a.png', false)).toBeNull()
    expect(safeImageUrl('http://cdn.example.test/a.png', false)).toBeNull()
  })

  it.each([
    'javascript:alert(1)',
    'JAVASCRIPT:alert(1)',
    'java\nscript:alert(1)',
    'data:image/svg+xml,<svg onload=alert(1)>',
    'data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==',
    'vbscript:msgbox(1)',
    'blob:https://cdn.example.test/1',
    'file:///etc/passwd',
    'ftp://cdn.example.test/a.png',
    '//evil.example/a.png',
    '/api/v1/patient/me',
    './a.png',
    '../a.png',
    'a.png',
    'https:a.png',
    'https:/a.png',
    'https:\\\\evil.example\\a.png',
    'HTTPS://cdn.example.test/a.png',
    ' https://cdn.example.test/a.png',
    'https://cdn.example.test/a.png ',
    'https://cdn.example.test/a\tb.png',
    'https://cdn.example.test/a\nb.png',
    'https://',
    'https:// /a.png',
    '',
  ])('refuses %j, in production and in development alike', (value) => {
    expect(safeImageUrl(value, false)).toBeNull()
    expect(safeImageUrl(value, true)).toBeNull()
  })

  it.each([null, undefined, 42, true, {}, [], ['https://cdn.example.test/a.png'], { href: 'https://cdn.example.test/a.png' }])(
    'refuses %j, which is not a string at all',
    (value) => {
      expect(safeImageUrl(value, false)).toBeNull()
    },
  )
})
