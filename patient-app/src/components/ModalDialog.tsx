import { useEffect, useId, useRef, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@atheris/ui'

interface ModalDialogProps {
  title: string
  /** Read out with the title when the dialog opens. */
  description?: ReactNode
  /** Asked for by Escape and by a press outside the dialog. Not called while `busy`. */
  onDismiss: () => void
  /** Something is under way that must not be walked away from: the dialog cannot be dismissed. */
  busy?: boolean
  /** The id of where focus goes on closing when what had it before is gone, or was nothing in particular. */
  fallbackFocusId?: string
  children: ReactNode
}

const TABBABLE = 'a[href], button, input, select, textarea, [tabindex]'

const isRadio = (element: Element | null): element is HTMLInputElement =>
  element instanceof HTMLInputElement && element.type === 'radio'

/** Two elements that Tab treats as one stop: the same element, or two radios of one group. */
const sameStop = (a: Element | null, b: Element | null) =>
  a === b || (isRadio(a) && isRadio(b) && a.name !== '' && a.name === b.name)

/** What Tab stops at inside `root`, in order. A group of radios is one stop, as it is to the browser. */
function tabStops(root: HTMLElement): HTMLElement[] {
  const reachable = [...root.querySelectorAll<HTMLElement>(TABBABLE)].filter(
    (element) => !element.hasAttribute('disabled') && element.tabIndex >= 0,
  )
  return reachable.filter((element) => {
    if (!isRadio(element) || element.name === '') return true
    const group = reachable.filter((other): other is HTMLInputElement => isRadio(other) && other.name === element.name)
    return element === (group.find((radio) => radio.checked) ?? group[0])
  })
}

/** While a dialog is open the page under it does not scroll. */
const SCROLL_LOCK = 'overflow-hidden'

/**
 * A modal dialog: a question the patient answers before going back to the
 * page. Mobile-first — it sits at the bottom of a small screen, within thumb
 * reach, and in the middle of a larger one.
 *
 * - It is a `dialog` that says it is modal, named by its title and described
 *   by its description.
 * - Focus moves to the title when it opens, cannot leave it by Tab or
 *   Shift+Tab, is pulled back if it gets out some other way, and returns to
 *   where it was when the dialog closes.
 * - Escape, or a press outside it, asks to dismiss it — the answer that
 *   changes nothing.
 */
export function ModalDialog({ title, description, onDismiss, busy = false, fallbackFocusId, children }: ModalDialogProps) {
  const titleId = useId()
  const descriptionId = useId()
  const panel = useRef<HTMLDivElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)

  useEffect(() => {
    const before =document.activeElement instanceof HTMLElement ? document.activeElement : null
    heading.current?.focus()
    document.documentElement.classList.add(SCROLL_LOCK)

    // Focus that gets out anyway — a screen reader's own cursor, a press on the page — comes back.
    const keepInside = (event: FocusEvent) => {
      const root = panel.current
      if (root && event.target instanceof Node && !root.contains(event.target)) heading.current?.focus()
    }
    document.addEventListener('focusin', keepInside)

    return () => {
      document.removeEventListener('focusin', keepInside)
      document.documentElement.classList.remove(SCROLL_LOCK)
      const usable = (element: HTMLElement | null): element is HTMLElement =>
        element !== null && element.isConnected && element !== document.body
      // Looked up now, as the dialog closes: it may not have been on the page when the dialog opened.
      const fallback = fallbackFocusId ? document.getElementById(fallbackFocusId) : null
      if (usable(before)) before.focus()
      else if (usable(fallback)) fallback.focus()
    }
  }, [fallbackFocusId])

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.stopPropagation()
      if (!busy) onDismiss()
      return
    }
    if (event.key !== 'Tab' || !panel.current) return
    const stops = tabStops(panel.current)
    const active = document.activeElement
    if (stops.length === 0) {
      event.preventDefault()
      heading.current?.focus()
      return
    }
    const first = stops[0]
    const last = stops[stops.length - 1]
    const isStop = stops.some((stop) => sameStop(stop, active))
    if (event.shiftKey ? !isStop || sameStop(active, first) : sameStop(active, last)) {
      event.preventDefault()
      const wrapTo = event.shiftKey ? last : first
      wrapTo.focus()
    }
  }

  return createPortal(
    // Presses on the backdrop itself dismiss; the dialog's own keys are handled as they bubble up to it.
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 sm:items-center sm:p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onDismiss()
      }}
      onKeyDown={onKeyDown}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        aria-busy={busy}
        className={cn(
          'bg-card max-h-[90dvh] w-full max-w-xl space-y-4 overflow-y-auto rounded-t-2xl border p-5',
          'pb-[max(1.25rem,env(safe-area-inset-bottom))] sm:rounded-2xl',
        )}
      >
        <h2 id={titleId} ref={heading} tabIndex={-1} className="font-display text-title-lg text-primary break-words">
          {title}
        </h2>
        {description && (
          <div id={descriptionId} className="text-body-lg text-on-surface break-words">
            {description}
          </div>
        )}
        {children}
      </div>
    </div>,
    document.body,
  )
}
