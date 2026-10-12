import type { PaymentMethod } from '@/api/billing'

export const PAYMENT_METHOD_LABEL: Record<PaymentMethod, string> = {
  cash: 'Cash',
  card: 'Card',
  upi: 'UPI',
  bank_transfer: 'Bank transfer',
  insurance: 'Insurance',
}

export const PAYMENT_METHODS = Object.keys(PAYMENT_METHOD_LABEL) as PaymentMethod[]

// Shared with the rest of the app; re-exported so billing code has one import.
export { MONEY_PATTERN, isPositiveMoney } from '@/lib/money'
export {
  apiErrorMessage as billingErrorMessage,
  fieldErrorsOf,
  splitFieldErrors,
  type FieldError,
} from '@/lib/apiErrors'
