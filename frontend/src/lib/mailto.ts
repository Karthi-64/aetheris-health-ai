/** A `mailto:` link that opens a message with the subject and body filled in. */
export function mailtoUrl({ to, subject, body }: { to: string; subject: string; body: string }): string {
  return `mailto:${to}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`
}

/** Hand a `mailto:` link to the visitor's email app. Nothing is sent by the page itself. */
export function openEmailApp(url: string): void {
  window.location.assign(url)
}
