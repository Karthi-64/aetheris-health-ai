import '@testing-library/jest-dom/vitest'

/**
 * jsdom implements neither the Pointer Capture API nor `scrollIntoView`, both
 * of which Radix calls when a `Select` opens. Without these stubs any test that
 * opens a dropdown dies with `target.hasPointerCapture is not a function`, and
 * the failure looks like a bug in the component rather than a gap in the DOM
 * implementation. Stubbed here once rather than in each test file.
 */
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false
  Element.prototype.setPointerCapture = () => {}
  Element.prototype.releasePointerCapture = () => {}
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {}
}
