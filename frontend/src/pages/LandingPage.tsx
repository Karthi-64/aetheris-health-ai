import { Link } from 'react-router-dom'
import {
  ArrowRight,
  Users,
  CalendarDays,
  Receipt,
  Building2,
  FlaskConical,
  Boxes,
  Hourglass,
  Pill,
  UserCog,
  ShieldCheck,
  Lock,
  BadgeCheck,
  Server,
  UserPlus,
  Stethoscope,
  LogIn,
} from 'lucide-react'
import MarketingNav from '@/components/layout/MarketingNav'
import MarketingFooter from '@/components/layout/MarketingFooter'
import {
  Accordion,
  AccordionItem,
  AccordionTrigger,
  AccordionContent,
} from '@/components/ui/accordion'

// Controls the application actually implements — not certifications, which it does not hold.
const SAFEGUARDS = [
  { icon: ShieldCheck, label: 'Role-based access' },
  { icon: Server, label: 'Per-hospital data separation' },
  { icon: Lock, label: 'Hashed passwords, optional MFA' },
  { icon: BadgeCheck, label: 'Append-only audit log' },
]

// The visit the platform carries end to end today.
const WORKFLOW = [
  { icon: UserPlus, title: 'Register', body: 'Create the patient record, or find it by name, phone, or MRN.' },
  { icon: CalendarDays, title: 'Schedule', body: "Book the patient into one of the doctor's open slots." },
  { icon: LogIn, title: 'Check in', body: 'Mark the arrival, or reschedule, cancel, or record a no-show.' },
  { icon: Stethoscope, title: 'Consult', body: 'The doctor starts and completes the consultation from the queue.' },
  { icon: Receipt, title: 'Bill', body: 'Issue the invoice and record payments and refunds against it.' },
]

const OUTCOMES = [
  {
    title: 'Less time hunting for context',
    body: "A patient's details, medical history, appointments, and invoices sit on one record.",
  },
  {
    title: 'Fewer things slip through',
    body: "The day's queue and the invoices awaiting payment sit on one dashboard, and every change is written to the audit log.",
  },
  {
    title: 'Faster front desk',
    body: 'Registration, scheduling, and billing share one workflow instead of three disconnected tools.',
  },
]

const FAQ = [
  {
    q: 'How is patient data protected?',
    a: "Access is scoped by role and permission, each hospital's data is kept separate, passwords are hashed and accounts can add a second factor, and changes are written to an append-only audit log. Aetheris has not been independently audited and does not currently claim HIPAA compliance or a SOC 2 report.",
  },
  {
    q: 'Which modules are available today?',
    a: 'Patients, doctors and departments, appointments, billing, laboratory orders and results, pharmacy prescribing and dispensing with medicine stock, inventory of supplies by location, reports and role dashboards, notifications, user and role management, hospital settings, and the audit log.',
  },
  {
    q: 'Does Aetheris include AI features today?',
    a: "One, and it is optional. When booking an appointment, staff can ask for an AI-suggested time slot, chosen from the doctor's open slots. It only suggests: a member of staff reviews it and books the appointment themselves. It is off unless it has been set up and enabled for your hospital, and it never makes a clinical decision.",
  },
  {
    q: 'Will it connect to our existing EHR and lab systems?',
    a: 'Not today. No integrations with other record, imaging, or lab systems have been built yet.',
  },
]

/** Module card — informational (public visitors are not signed in), no app link. */
function ModuleCard({
  icon: Icon,
  title,
  body,
  className,
  dark,
}: {
  icon: typeof Users
  title: string
  body: string
  className?: string
  dark?: boolean
}) {
  return (
    <div
      className={[
        'flex flex-col gap-3 rounded-2xl p-6',
        dark ? 'bg-primary-container relative overflow-hidden' : 'neo-extruded bg-surface',
        className ?? '',
      ].join(' ')}
    >
      {dark && (
        <div className="bg-secondary-container/20 pointer-events-none absolute -top-10 -right-10 size-40 rounded-full blur-2xl" />
      )}
      <span
        className={[
          'flex size-11 items-center justify-center rounded-xl',
          dark ? 'bg-secondary/20 text-secondary-container' : 'bg-secondary/10 text-secondary',
        ].join(' ')}
      >
        <Icon className="size-6" />
      </span>
      <h3
        className={[
          'font-display text-title-lg font-bold',
          dark ? 'text-white' : 'text-primary',
        ].join(' ')}
      >
        {title}
      </h3>
      <p
        className={[
          'font-body text-body-sm',
          dark ? 'text-white/70' : 'text-on-surface-variant',
        ].join(' ')}
      >
        {body}
      </p>
    </div>
  )
}

export default function LandingPage() {
  return (
    <div className="min-h-screen overflow-x-hidden">
      <MarketingNav />

      {/* ── Hero: asymmetric split ─────────────────────────────────────────── */}
      <section className="px-container-padding relative mx-auto max-w-7xl pt-28 pb-16 md:pt-40">
        <div className="from-secondary-fixed/20 absolute inset-0 -z-10 rounded-3xl bg-gradient-to-br to-transparent opacity-50 blur-3xl" />
        <div className="gap-gutter grid items-center md:grid-cols-12">
          <div className="space-y-6 md:col-span-6">
            <div className="glassmorphism inline-block rounded-full px-4 py-2">
              <span className="font-label text-label-caps text-secondary font-bold tracking-wider">
                HOSPITAL MANAGEMENT PLATFORM
              </span>
            </div>
            <h1 className="font-display text-gradient text-[2rem] leading-[1.1] font-extrabold tracking-tight sm:text-4xl md:text-headline-xl">
              Run your whole hospital on one connected platform.
            </h1>
            <p className="font-body text-body-md text-on-surface-variant max-w-lg">
              Patients, appointments, billing, laboratory, pharmacy, and inventory in a single
              system that your front desk, doctors, lab, pharmacy, stores, and billing staff all
              share.
            </p>
            <div className="flex flex-wrap gap-4 pt-2">
              <Link
                to="/contact"
                className="neo-extruded bg-primary text-on-primary text-body-md rounded-xl px-8 py-4 font-bold transition-transform duration-300 hover:-translate-y-0.5 active:translate-y-0"
              >
                Book a demo
              </Link>
              <a
                href="#modules"
                className="glassmorphism text-primary text-body-md hover:bg-secondary/10 flex items-center gap-2 rounded-xl px-8 py-4 font-bold transition-colors"
              >
                Explore the platform
                <ArrowRight className="size-5" />
              </a>
            </div>
          </div>

          {/* Asset: honest 3-step process diagram (not a fake screenshot) */}
          <div className="relative md:col-span-6">
            <div className="from-secondary-fixed to-primary-fixed absolute -inset-4 z-0 rounded-[2rem] bg-gradient-to-tr opacity-20 blur-2xl" />
            <div className="glassmorphism relative z-10 rounded-[2rem] p-8">
              <p className="font-label text-label-caps text-outline mb-6">
                FROM ARRIVAL TO PAYMENT
              </p>
              <ol className="space-y-2">
                {[
                  { icon: Users, title: 'Register', body: 'Open a patient record and find it again by name, phone, or MRN.' },
                  { icon: CalendarDays, title: 'Schedule', body: "Book into a doctor's open slots and run the day's queue." },
                  { icon: Receipt, title: 'Bill', body: 'Invoice the visit and record payments against it.' },
                ].map((step, i, arr) => (
                  <li key={step.title} className="relative flex gap-4 pb-6 last:pb-0">
                    {i < arr.length - 1 && (
                      <span className="bg-outline-variant/40 absolute top-12 left-[23px] h-[calc(100%-2.5rem)] w-px" />
                    )}
                    <span className="neo-extruded bg-secondary/10 text-secondary z-10 flex size-12 shrink-0 items-center justify-center rounded-full">
                      <step.icon className="size-6" />
                    </span>
                    <div className="pt-1">
                      <h3 className="font-display text-title-lg text-primary font-bold">
                        {step.title}
                      </h3>
                      <p className="font-body text-body-sm text-on-surface-variant mt-1">
                        {step.body}
                      </p>
                    </div>
                  </li>
                ))}
              </ol>
            </div>
          </div>
        </div>
      </section>

      {/* ── Safeguards band ────────────────────────────────────────────────── */}
      <section className="px-container-padding mx-auto max-w-7xl">
        <div className="neo-pressed bg-surface flex flex-wrap items-center justify-center gap-x-10 gap-y-4 rounded-2xl px-6 py-5">
          {SAFEGUARDS.map(({ icon: Icon, label }) => (
            <div key={label} className="text-on-surface-variant flex items-center gap-2">
              <Icon className="text-secondary size-5" />
              <span className="font-label text-label-caps">{label}</span>
            </div>
          ))}
        </div>
      </section>

      {/* ── Modules: bento (1 wide dark feature + 4 tiles) ─────────────────── */}
      <section id="modules" className="px-container-padding mx-auto mt-24 max-w-7xl scroll-mt-28">
        <div className="mb-10 max-w-2xl">
          <h2 className="font-display text-headline-lg text-primary">
            The front desk, the consulting room, the lab, the pharmacy, stores, and billing in
            one place.
          </h2>
          <p className="font-body text-body-md text-on-surface-variant mt-3">
            Each team works in its own module and shares the same patient record, so nothing is
            re-entered between them.
          </p>
        </div>

        {/* Tall roadmap card (col 1) + 2x2 modules (cols 2-3), then Laboratory, Pharmacy, Inventory and a wide Administration card */}
        <div className="grid gap-5 md:grid-cols-3">
          <ModuleCard
            dark
            icon={Hourglass}
            title="On the roadmap"
            body="More AI assistance is planned. Today the only AI feature is an optional slot suggestion when booking, where it has been enabled."
            className="md:row-span-2"
          />
          <ModuleCard
            icon={Users}
            title="Patients"
            body="A searchable registry with each patient's details, medical history, appointments, and invoices."
          />
          <ModuleCard
            icon={CalendarDays}
            title="Appointments"
            body="Slot booking against doctor availability, rescheduling, and the daily queue from check-in to completion."
          />
          <ModuleCard
            icon={Receipt}
            title="Billing"
            body="Invoices, payments, and refunds tied to each visit."
          />
          <ModuleCard
            icon={Building2}
            title="Doctors & departments"
            body="Doctor profiles with specialty and consultation fee, listed by department. A doctor's open slots are offered when booking."
          />
          <ModuleCard
            icon={FlaskConical}
            title="Laboratory"
            body="Tests ordered from a visit, sample collection, result entry checked against the hospital's own reference ranges, release, and recorded corrections."
          />
          <ModuleCard
            icon={Pill}
            title="Pharmacy"
            body="Medicines prescribed from a visit and dispensed from batch-tracked stock, with the charge added to the patient's invoice. A medicine catalog, vendors, and purchase orders for restocking."
          />
          <ModuleCard
            icon={Boxes}
            title="Inventory"
            body="Supplies tracked by location and batch: recording what is used, transfers between locations, count corrections, a full movement ledger, low-stock flags against each item's reorder point, and purchase orders."
          />
          <ModuleCard
            icon={UserCog}
            title="Administration"
            body="Users and roles with permission-based access, in-app notifications, hospital settings, and an audit log that can be exported."
            className="md:col-span-3"
          />
        </div>
      </section>

      {/* ── Workflow: horizontal clinical journey ──────────────────────────── */}
      <section className="px-container-padding mx-auto mt-24 max-w-7xl">
        <div className="mb-10 max-w-2xl">
          <h2 className="font-display text-headline-lg text-primary">
            One patient journey, start to finish.
          </h2>
        </div>
        <ol className="relative grid gap-8 md:grid-cols-5">
          <span
            aria-hidden
            className="bg-outline-variant/40 absolute top-6 right-6 left-6 hidden h-px md:block"
          />
          {WORKFLOW.map((step, i) => (
            <li key={step.title} className="relative flex flex-col gap-3">
              <span className="neo-extruded bg-surface text-secondary z-10 flex size-12 items-center justify-center rounded-full">
                <step.icon className="size-6" />
              </span>
              <div>
                <p className="font-label text-label-caps text-outline">
                  {String(i + 1).padStart(2, '0')}
                </p>
                <h3 className="font-display text-title-lg text-primary font-bold">{step.title}</h3>
                <p className="font-body text-body-sm text-on-surface-variant mt-1">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {/* ── Outcomes: divider layout ───────────────────────────────────────── */}
      <section className="px-container-padding mx-auto mt-24 max-w-7xl">
        <div className="grid gap-8 md:grid-cols-3">
          {OUTCOMES.map((o) => (
            <div key={o.title} className="border-outline-variant/40 border-t pt-6">
              <h3 className="font-display text-title-lg text-primary font-bold">{o.title}</h3>
              <p className="font-body text-body-sm text-on-surface-variant mt-2">{o.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* ── FAQ: accordion ─────────────────────────────────────────────────── */}
      <section className="px-container-padding mx-auto mt-24 max-w-3xl">
        <h2 className="font-display text-headline-lg text-primary mb-10 text-center">
          Questions hospitals ask us
        </h2>
        <Accordion type="single" collapsible className="flex flex-col gap-4">
          {FAQ.map((item, i) => (
            <AccordionItem key={item.q} value={`faq-${i}`}>
              <AccordionTrigger>{item.q}</AccordionTrigger>
              <AccordionContent>{item.a}</AccordionContent>
            </AccordionItem>
          ))}
        </Accordion>
      </section>

      {/* ── Final CTA ──────────────────────────────────────────────────────── */}
      <section className="px-container-padding mx-auto mt-24 max-w-7xl">
        <div className="glassmorphism relative overflow-hidden rounded-3xl px-8 py-16 text-center">
          <div className="from-secondary-fixed/30 absolute inset-0 -z-10 bg-gradient-to-br to-transparent blur-3xl" />
          <h2 className="font-display text-headline-lg text-gradient mx-auto max-w-2xl">
            See Aetheris run your hospital.
          </h2>
          <p className="font-body text-body-md text-on-surface-variant mx-auto mt-3 max-w-md">
            Walk through the platform with our team and map it to your departments.
          </p>
          <div className="mt-8 flex justify-center">
            <Link
              to="/contact"
              className="neo-extruded bg-primary text-on-primary text-body-md inline-flex items-center gap-2 rounded-xl px-8 py-4 font-bold transition-transform duration-300 hover:-translate-y-0.5 active:translate-y-0"
            >
              Book a demo
              <ArrowRight className="size-5" />
            </Link>
          </div>
        </div>
      </section>

      <MarketingFooter />
    </div>
  )
}
