/**
 * An image address the app is willing to put in `<img src>`, or `null`.
 *
 * The address comes from a hospital's settings, so it is treated as untrusted
 * whatever the server did with it. Only an absolute `https://` address is
 * accepted (`http://` too in a development build, where the local API serves
 * it). Everything else is refused: `javascript:` and `data:`, a relative or
 * protocol-relative path — which would make the browser send a request to this
 * origin or pick the scheme itself — and anything that does not parse.
 */
export function safeImageUrl(value: unknown, allowHttp: boolean = import.meta.env.DEV): string | null {
  if (typeof value !== 'string') return null
  const isHttps = value.startsWith('https://')
  if (!isHttps && !(allowHttp && value.startsWith('http://'))) return null
  // A browser drops tabs and line breaks inside an address before reading it;
  // an address that relies on that is not one a hospital typed.
  if (/\s/.test(value)) return null
  try {
    const url = new URL(value)
    const expected = isHttps ? 'https:' : 'http:'
    return url.protocol === expected && url.hostname !== '' ? url.href : null
  } catch {
    return null
  }
}
