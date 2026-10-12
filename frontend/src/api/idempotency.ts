/**
 * A fresh value for the `Idempotency-Key` header (`docs/06-API_STANDARDS.md` §12).
 *
 * `crypto.randomUUID` only exists in secure contexts, so a dev build opened
 * over plain HTTP on a LAN address falls back to `getRandomValues`.
 */
export function newIdempotencyKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
}
