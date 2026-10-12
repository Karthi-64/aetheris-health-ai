export interface LegalDoc {
  slug: string
  title: string
  updated: string
  intro: string
  sections: { heading: string; body: string[] }[]
}

const CONTACT_LINE =
  'Questions about this page can be sent to our privacy team via the Contact page.'

export const privacyDoc: LegalDoc = {
  slug: 'privacy',
  title: 'Privacy Policy',
  updated: 'October 2026',
  intro:
    'This policy explains what information Aetheris Health AI collects, how we use it, and the choices you have. It applies to our marketing site and the Aetheris platform.',
  sections: [
    {
      heading: 'Information we collect',
      body: [
        'Account information you provide, such as your name, work email, and organization.',
        'Usage data about how the platform is used, collected to keep the service secure and reliable.',
      ],
    },
    {
      heading: 'Protected health information',
      body: [
        "Patient data entered into Aetheris belongs to the hospital that entered it and is kept separate from every other hospital's data. It is handled only as needed to provide the service to that hospital.",
      ],
    },
    {
      heading: 'How we use information',
      body: [
        'To provide, maintain, and improve the platform, to secure it against misuse, and to support your team.',
        "We do not sell personal information. Aetheris has one optional AI feature: slot suggestions when booking an appointment. It is off unless it has been set up and enabled for a hospital. When staff use it, the date and the open and unavailable times in the chosen doctor's day are sent to an external AI model provider so that it can suggest one. No patient name, record number, contact detail or medical information is sent, and patient data is not used to train a model.",
      ],
    },
    {
      heading: 'Data security',
      body: [
        'Access is role-based, passwords are stored hashed, and changes to records are written to an audit log. See the Security page for the safeguards the platform implements.',
      ],
    },
    {
      heading: 'Data retention',
      body: [
        'We retain information for as long as your account is active or as needed to provide the service, then delete or de-identify it in line with your agreement and applicable law.',
      ],
    },
    {
      heading: 'Your rights',
      body: [
        'Depending on your location, you may request access to, correction of, or deletion of your personal information. Contact us to exercise these rights.',
      ],
    },
    {
      heading: 'Changes to this policy',
      body: [
        'We may update this policy from time to time. Material changes will be communicated to account administrators.',
        CONTACT_LINE,
      ],
    },
  ],
}

export const termsDoc: LegalDoc = {
  slug: 'terms',
  title: 'Terms of Service',
  updated: 'October 2026',
  intro:
    'These terms govern your access to and use of the Aetheris Health AI platform. By using the service, your organization agrees to them.',
  sections: [
    {
      heading: 'The service',
      body: [
        'Aetheris provides hospital management software for patient registration and records, a directory of doctors and departments, appointment scheduling, billing, laboratory orders and results, pharmacy prescribing and dispensing, inventory of supplies, in-app notifications, user and role management, hospital settings, and an audit log.',
      ],
    },
    {
      heading: 'Clinical responsibility',
      body: [
        "Aetheris is an administrative and record-keeping tool. The prescriptions it stores, and the laboratory results it marks against a hospital's own reference ranges, are records and not clinical advice.",
        'It does not diagnose, recommend treatment, or make clinical decisions. A qualified clinician is responsible for every diagnosis and treatment decision.',
      ],
    },
    {
      heading: 'Accounts and eligibility',
      body: [
        'You are responsible for keeping account credentials secure and for activity under your account. Accounts are for authorized hospital staff only.',
      ],
    },
    {
      heading: 'Acceptable use',
      body: [
        'You agree not to misuse the service, attempt to access it without authorization, or use it in violation of applicable law or your organization policies.',
      ],
    },
    {
      heading: 'Intellectual property',
      body: [
        'Aetheris and its software are owned by Aetheris Health AI. These terms do not transfer any ownership in the platform to you.',
      ],
    },
    {
      heading: 'Disclaimers and liability',
      body: [
        'The service is provided on an as-available basis. To the extent permitted by law, Aetheris is not liable for indirect or consequential damages arising from use of the service.',
      ],
    },
    {
      heading: 'Changes to these terms',
      body: [
        'We may update these terms and will notify account administrators of material changes.',
        CONTACT_LINE,
      ],
    },
  ],
}

/**
 * The security page. It lists controls the application implements and says plainly what
 * has not been done: no audit, certification or compliance claim is made.
 */
export const securityDoc: LegalDoc = {
  slug: 'security',
  title: 'Security',
  updated: 'October 2026',
  intro:
    'This page describes the safeguards the Aetheris platform implements today, and what it does not yet claim.',
  sections: [
    {
      heading: 'Access control',
      body: [
        'Every action in the platform requires a signed-in account, and what an account can see and do is decided by the roles and permissions a hospital administrator assigns to it. The server enforces those permissions on every request.',
      ],
    },
    {
      heading: 'Separation between hospitals',
      body: [
        "Each hospital's records are scoped to that hospital. An account can only reach the data of the hospital it belongs to.",
      ],
    },
    {
      heading: 'Sign-in protection',
      body: [
        'Passwords are stored as Argon2id hashes, never in readable form. Repeated failed sign-ins lock an account for a period, sessions use short-lived access tokens, and an account can add a second factor with an authenticator app.',
      ],
    },
    {
      heading: 'Audit log',
      body: [
        'Changes made in the platform are written to an audit log that records who did what and when. Entries cannot be edited or deleted through the application, and administrators with the right permission can search and export them.',
      ],
    },
    {
      heading: 'What we do not claim',
      body: [
        'Aetheris has not been independently audited or certified. We do not currently claim HIPAA compliance, hold a SOC 2 report, or offer a Business Associate Agreement.',
        'Encryption of data in transit and at rest depends on how and where the platform is deployed, and is not something this page can state for you.',
        CONTACT_LINE,
      ],
    },
  ],
}

export const legalDocs: Record<string, LegalDoc> = {
  privacy: privacyDoc,
  terms: termsDoc,
  security: securityDoc,
}
