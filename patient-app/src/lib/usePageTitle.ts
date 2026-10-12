import { useEffect } from 'react'

const APP_NAME = 'Atheris Health'

/** Name the page in the tab and to assistive technology (WCAG 2.4.2). */
export function usePageTitle(title: string) {
  useEffect(() => {
    document.title = `${title} · ${APP_NAME}`
  }, [title])
}
