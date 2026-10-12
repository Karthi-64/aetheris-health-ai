"""Patient App services (``docs/modules/15-patient-app.md``).

Everything a patient principal can do goes through this package. It is kept
apart from the staff services on purpose: a patient is authorised by *who
they are and which record is theirs*, never by a role or a permission, and no
module here imports the staff authentication service or its dependencies.

- :mod:`~app.services.patient_app.patient_auth_service` — one-time codes and sessions
- :mod:`~app.services.patient_app.otp_service` — the code itself: issue, check, consume
- :mod:`~app.services.patient_app.patient_account_service` — ``GET /patient/me``
- :mod:`~app.services.patient_app.record_link_service` — linking to, or registering, a record
- :mod:`~app.services.patient_app.hospital_gate` — which hospitals are open to patients
- :mod:`~app.services.patient_app.hospital_directory_service` — hospital discovery
- :mod:`~app.services.patient_app.doctor_directory_service` — doctor discovery
- :mod:`~app.services.patient_app.availability_service` — when a doctor can be booked
- :mod:`~app.services.patient_app.booking_policy` — the per-hospital booking policy
- :mod:`~app.services.patient_app.booking_service` — booking an appointment
- :mod:`~app.services.patient_app.appointments_service` — the patient's own appointments
- :mod:`~app.services.patient_app.consent_service` — purpose consent and the policy gate
- :mod:`~app.services.patient_app.access_grant_service` — record access grants
- :mod:`~app.services.patient_app.patient_authorization` — the one entry point that turns an
  authenticated account into a patient context at a hospital
"""
