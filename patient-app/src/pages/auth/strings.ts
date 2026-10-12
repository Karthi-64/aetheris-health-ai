/** User-facing strings of the sign-in screens (one module per feature, spec 25.6). */
export const authStrings = {
  loginTitle: 'Sign in',
  loginHeading: 'Sign in with your mobile number',
  loginIntro: 'We will text you a 6-digit code to confirm it is you.',
  phoneLabel: 'Mobile number',
  phoneHint: 'Include your country code, for example +91 98765 43210.',
  phoneRequired: 'Enter your mobile number.',
  phoneInvalid: 'Enter your full number starting with the country code, for example +91 98765 43210.',
  phoneNotAccepted: 'We cannot send a code to this number. Check the country code and the number.',
  sendCode: 'Send code',
  sendingCode: 'Sending…',
  sessionEnded: 'You have been signed out. Please sign in again.',

  verifyTitle: 'Enter your code',
  verifyHeading: 'Enter the 6-digit code',
  verifySentTo: 'If the number is correct, a code is on its way to',
  codeLabel: '6-digit code',
  codeFormat: 'Enter the 6 digits from the text message.',
  verify: 'Verify and continue',
  verifying: 'Checking…',
  // One message for every way a code can fail — wrong, expired, used, too many tries.
  codeRejected: 'That code did not work. Check it and try again, or ask for a new code.',
  resend: 'Send a new code',
  resendIn: (seconds: number) => `Send a new code in ${seconds}s`,
  resent: 'A new code is on its way.',
  changeNumber: 'Use a different number',

  // One message for every limit, whichever was reached.
  throttled: 'Too many attempts. Please wait a few minutes and try again.',
  unavailable: 'We cannot send codes right now. Please try again later.',
  failed: 'Something went wrong. Check your connection and try again.',
} as const
