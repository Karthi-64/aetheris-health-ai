import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigationType } from 'react-router-dom'
import { cn } from '@atheris/ui'

interface PageHeadingProps {
  children: ReactNode
  className?: string
  /**
   * Take focus however the page was reached. For a heading that appears in
   * answer to something the patient just did on the page — an outcome — where
   * leaving focus on a control that is gone would lose their place.
   */
  focusOnMount?: boolean
}

/**
 * A page's `<h1>`. When the patient arrives by a link inside the app it takes
 * focus, so a keyboard or screen-reader user starts at the top of the new page
 * instead of where the last one left them (WCAG 2.4.3). It also scrolls into
 * view, clear of the sticky header.
 *
 * Not on first load, reload, back or forward: there the browser places the
 * patient itself, and a focus ring on a heading nobody asked for is noise.
 */
export function PageHeading({ children, className, focusOnMount = false }: PageHeadingProps) {
  const heading = useRef<HTMLHeadingElement>(null)
  const navigationType = useNavigationType()
  // Decided once, as the heading appears: later changes to the query string on
  // the same page are navigations too, and must not pull focus back up here.
  const [arrivedByLink] = useState(focusOnMount || navigationType !== 'POP')

  useEffect(() => {
    if (arrivedByLink) heading.current?.focus()
  }, [arrivedByLink])

  return (
    <h1
      ref={heading}
      tabIndex={-1}
      className={cn('font-display text-headline-md text-primary scroll-mt-24 break-words', className)}
    >
      {children}
    </h1>
  )
}
