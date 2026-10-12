import { Link } from 'react-router-dom'

const LINKS = [
  { label: 'Privacy Policy', to: '/privacy' },
  { label: 'Terms of Service', to: '/terms' },
  { label: 'Security', to: '/security' },
  { label: 'Contact', to: '/contact' },
]

/** Public footer shared across the marketing pages. */
export default function MarketingFooter() {
  return (
    <footer className="mt-section-margin border-outline-variant/20 w-full border-t py-8">
      <div className="px-container-padding mx-auto flex max-w-7xl flex-col items-center justify-between gap-4 md:flex-row">
        <p className="font-body text-body-sm text-primary font-bold">
          © 2026 Aetheris Health AI · Hospital Management Platform.
        </p>
        <div className="flex flex-wrap justify-center gap-6">
          {LINKS.map((item) => (
            <Link
              key={item.label}
              to={item.to}
              className="font-body text-body-sm text-outline hover:text-secondary underline-offset-4 transition-colors hover:underline"
            >
              {item.label}
            </Link>
          ))}
        </div>
      </div>
    </footer>
  )
}
