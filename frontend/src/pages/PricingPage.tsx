import { Link } from 'react-router-dom'
import { ArrowRight, CheckCircle2, Hourglass } from 'lucide-react'
import MarketingNav from '@/components/layout/MarketingNav'
import MarketingFooter from '@/components/layout/MarketingFooter'

/** What a hospital gets today — each of these is a working module in the product. */
const INCLUDED = [
  'Patient registry and records',
  'Doctor directory by department, with open slots when booking',
  'Appointment booking and the daily queue',
  'Invoices, payments, and refunds',
  'Laboratory orders and results',
  'Pharmacy prescribing, dispensing, and medicine stock',
  'Inventory of supplies by location, with purchase orders',
  'Reports and role dashboards, with CSV export',
  'In-app notifications',
  'Users, roles, and permission-based access',
  'Audit log with export',
]

/** Planned, and not available yet. */
const PLANNED = ['AI assistance beyond slot suggestions']

/**
 * Pricing. There are no published plans or prices — the product has no
 * subscription or plan tiers — so this page says what is included today and
 * points to a conversation rather than listing tiers that do not exist.
 */
export default function PricingPage() {
  return (
    <div className="relative min-h-screen overflow-x-hidden">
      {/* Floating decorative shapes */}
      <div className="pointer-events-none absolute inset-0 -z-10 overflow-hidden">
        <div className="bg-primary-fixed/40 absolute top-[10%] left-[-5%] h-[400px] w-[400px] rounded-full blur-3xl" />
        <div className="bg-secondary-fixed/30 absolute right-[-10%] bottom-[10%] h-[500px] w-[500px] rounded-full blur-3xl" />
      </div>

      <MarketingNav />

      <main className="px-container-padding mx-auto max-w-5xl pt-28 pb-16 md:pt-36">
        <div className="mb-14 text-center">
          <h1 className="font-display text-headline-xl text-primary mb-3">Pricing</h1>
          <p className="font-body text-body-md text-on-surface-variant mx-auto max-w-2xl">
            Aetheris does not have published plans yet. Tell us about your hospital and we will
            talk through what fits.
          </p>
        </div>

        <div className="grid grid-cols-1 items-stretch gap-6 md:grid-cols-5 lg:gap-8">
          <section
            aria-labelledby="included-heading"
            className="glassmorphism shadow-glass-panel rounded-3xl p-8 md:col-span-3"
          >
            <h2 id="included-heading" className="font-display text-headline-md text-primary mb-2 font-bold">
              Included today
            </h2>
            <p className="font-body text-body-sm text-on-surface-variant mb-6">
              The modules that are built and working in the product.
            </p>
            <ul className="space-y-4">
              {INCLUDED.map((item) => (
                <li key={item} className="flex items-center gap-3">
                  <CheckCircle2 className="text-secondary size-5 shrink-0" aria-hidden />
                  <span className="font-body text-body-sm text-on-surface">{item}</span>
                </li>
              ))}
            </ul>
          </section>

          <section
            aria-labelledby="planned-heading"
            className="neo-extruded bg-surface flex flex-col rounded-3xl p-8 md:col-span-2"
          >
            <h2 id="planned-heading" className="font-display text-headline-md text-primary mb-2 font-bold">
              On the roadmap
            </h2>
            <p className="font-body text-body-sm text-on-surface-variant mb-6">
              Planned, and not available yet.
            </p>
            <ul className="flex-1 space-y-4">
              {PLANNED.map((item) => (
                <li key={item} className="flex items-center gap-3">
                  <Hourglass className="text-outline size-5 shrink-0" aria-hidden />
                  <span className="font-body text-body-sm text-on-surface-variant">{item}</span>
                </li>
              ))}
            </ul>
          </section>
        </div>

        <div className="mt-12 flex justify-center">
          <Link
            to="/contact"
            className="neo-extruded bg-primary text-on-primary text-body-md inline-flex items-center gap-2 rounded-xl px-8 py-4 font-bold transition-transform duration-300 hover:-translate-y-0.5 active:translate-y-0"
          >
            Talk to us
            <ArrowRight className="size-5" />
          </Link>
        </div>
      </main>

      <MarketingFooter />
    </div>
  )
}
