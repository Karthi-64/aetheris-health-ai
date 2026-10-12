/** E.164: a plus, a country code that does not start with 0, 8–15 digits in all. */
const E164 = /^\+[1-9]\d{7,14}$/

/**
 * The phone number a patient typed, as E.164 (`+919876543210`), or `null`
 * when it is not one. Spaces, dashes, dots and brackets are how people write
 * numbers and are dropped; a missing country code is not guessed.
 */
export function normalisePhone(input: string): string | null {
  const compact = input.replace(/[\s\-.()]/g, '')
  return E164.test(compact) ? compact : null
}
