import { useState } from 'react'
import { CircleCheck } from 'lucide-react'
import { Link, useLocation } from 'react-router-dom'
import { Button } from '@atheris/ui'
import { usePageTitle } from '@/lib/usePageTitle'
import { LinkDetailsForm } from '@/pages/hospitals/LinkDetailsForm'
import { hospitalCodeFrom } from '@/pages/hospitals/linkState'
import type { LinkAttempt, LinkSuccess } from '@/pages/hospitals/outcomes'
import { RegisterForm } from '@/pages/hospitals/RegisterForm'
import { linkStrings as S } from '@/pages/hospitals/strings'

type Step =
  | { name: 'details' }
  | { name: 'register'; attempt: LinkAttempt }
  | { name: 'done'; outcome: LinkSuccess }

const DONE: Record<LinkSuccess, { title: string; body: string }> = {
  linked: { title: S.linkedTitle, body: S.linkedBody },
  already_linked: { title: S.alreadyLinkedTitle, body: S.alreadyLinkedBody },
  registered: { title: S.registeredTitle, body: S.registeredBody },
}

/**
 * Link this account to a hospital record: details first; registration as a
 * confirmed second step only when the hospital has no record to link.
 *
 * Arriving from a hospital's page, that hospital's code is already filled in;
 * it is a starting value, and the patient can still type another.
 */
export function LinkPatientPage() {
  usePageTitle(S.title)
  const [step, setStep] = useState<Step>({ name: 'details' })
  const hospitalCode = hospitalCodeFrom(useLocation().state)

  if (step.name === 'done') {
    const { title, body } = DONE[step.outcome]
    return (
      <div role="status" className="bg-card space-y-4 rounded-2xl border p-6 text-center">
        <CircleCheck className="text-stable mx-auto size-12" aria-hidden />
        <h1 className="font-display text-headline-md text-primary">{title}</h1>
        <p className="text-body-lg text-on-surface-variant">{body}</p>
        <Button asChild size="touch" className="w-full">
          <Link to="/">{S.goHome}</Link>
        </Button>
      </div>
    )
  }

  if (step.name === 'register') {
    return (
      <RegisterForm
        attempt={step.attempt}
        onBack={() => setStep({ name: 'details' })}
        onDone={() => setStep({ name: 'done', outcome: 'registered' })}
      />
    )
  }

  return (
    <LinkDetailsForm
      initialHospitalCode={hospitalCode}
      onDone={(outcome) => setStep({ name: 'done', outcome })}
      onRegister={(attempt) => setStep({ name: 'register', attempt })}
    />
  )
}
