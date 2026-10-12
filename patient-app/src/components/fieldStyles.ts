/**
 * The Patient App's text control: 44 px tall and 16 px type (smaller type makes
 * mobile browsers zoom the page on focus), a plain bordered surface rather
 * than the Hospital app's sunken one.
 */
export const fieldControlClass = [
  'block min-h-11 w-full rounded-xl border border-outline bg-surface-container-lowest px-4 py-2.5',
  'text-base text-on-surface placeholder:text-outline',
  'transition-colors hover:border-on-surface-variant',
  'aria-[invalid=true]:border-error aria-[invalid=true]:ring-1 aria-[invalid=true]:ring-error',
  'disabled:cursor-not-allowed disabled:opacity-60',
].join(' ')

/** Class for the box inside a `CheckboxField`. */
export const checkboxClass = 'accent-secondary mt-0.5 size-5 shrink-0 cursor-pointer'
