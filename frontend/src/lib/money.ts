/**
 * What the API accepts as an amount: digits with at most two decimals
 * (docs/18-API_CONTRACTS.md §6.2). This only checks that the text is a money
 * value — whether it is too much is the server's call. Amounts are compared
 * and sent as text, so no float is ever involved.
 */
export const MONEY_PATTERN = /^\d+(\.\d{1,2})?$/

/** True for a well-formed amount above zero. */
export function isPositiveMoney(value: string): boolean {
  return MONEY_PATTERN.test(value) && /[1-9]/.test(value)
}
