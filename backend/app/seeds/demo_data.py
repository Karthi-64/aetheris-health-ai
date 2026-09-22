"""Realistic demo data for development and the frontend integration demo.

Called by :func:`app.seeds.seed.seed_database` after the permissions, roles,
hospital and demo users exist. It is not a second seed mechanism: there is one
entrypoint (``make -C backend seed``) and this module is a section of it,
separated only because the catalogue data below would otherwise triple the
length of ``seed.py``.

**Everyone here is fictional.** Names, phone numbers, licence numbers and
addresses are invented for demonstration. No real patient information is in
this repository, and none may ever be added to it.

**Idempotency.** Every entity is looked up by a stable natural key before it is
created, matching the pattern the permissions/roles seed already uses:

===============  ==========================================================
Entity           Natural key
===============  ==========================================================
Department       ``(hospital_id, code)`` — the table's unique constraint
User             ``(hospital_id, email)``
Doctor           ``(hospital_id, license_number)``
Availability     presence of any row for the doctor (the set is replaced whole)
Leave            ``(doctor_id, starts_at)``
Patient          ``(hospital_id, first_name, last_name, date_of_birth)``
Appointment      ``(hospital_id, idempotency_key)`` — the partial unique index
                 from migration 0008, i.e. the mechanism the booking API
                 already uses for retries
===============  ==========================================================

So a second run creates nothing, allocates no further MRNs, and leaves every
relationship pointing where it did. Nothing bypasses a database constraint;
in particular the seeded appointments are laid out so that no two live ones
share a doctor and a time, which the ``no_overlap_per_doctor`` exclusion
constraint would otherwise reject at COMMIT.

**Times.** Appointments are anchored to the day the seed first runs — some
yesterday, some today, some in the next two days — so a demo always has a
plausible "today". They are expressed as Asia/Kolkata wall-clock and stored in
UTC, and they land on the doctors' published slot boundaries so the
``/doctors/{id}/slots`` read model shows them as ``booked``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select

from app.core.security import hash_password
from app.models.appointment import (
    Appointment,
    AppointmentStatus,
    AppointmentStatusHistory,
    AppointmentType,
)
from app.models.department import Department
from app.models.doctor import Doctor, DoctorAvailability, DoctorLeave
from app.models.patient import BloodGroup, Gender, Patient
from app.models.user import User, UserRole, UserStatus
from app.repositories.mrn_sequence_repository import MrnSequenceRepository
from app.services.mrn_service import MRNService

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.hospital import Hospital
    from app.models.role import Role

logger = structlog.get_logger(__name__)

#: The demo hospital operates in this zone (``Hospital.timezone`` default).
#: Availability and appointment times below are wall-clock in it.
CLINIC_TZ = ZoneInfo("Asia/Kolkata")

#: Shared password for every seeded clinical user. Development only — the
#: hospital's real users are invited through the normal flow.
DEMO_PASSWORD = "Doctor@1234567"

#: Half-hour consulting slots, 09:00–13:00 and 14:00–17:00, Monday to Friday.
#: ``day_of_week`` is 0=Monday .. 6=Sunday (module spec §5.2).
_STANDARD_WEEK: list[tuple[int, time, time, int]] = [
    (day, time(9, 0), time(13, 0), 30) for day in range(5)
] + [(day, time(14, 0), time(17, 0), 30) for day in range(5)]

#: A shorter week for the paediatrician — mornings only, and Saturday clinic.
_MORNINGS_AND_SATURDAY: list[tuple[int, time, time, int]] = [
    (day, time(9, 0), time(12, 30), 20) for day in range(5)
] + [(5, time(9, 0), time(12, 0), 20)]


# ── Catalogue data ───────────────────────────────────────────────────────────
# Format: (code, name, description, extension, email, location, is_active)

DEPARTMENTS: list[tuple[str, str, str, str, str, str, bool]] = [
    (
        "CARD",
        "Cardiology",
        "Heart and vascular care, including ECG and echocardiography.",
        "201",
        "cardiology@demohospital.com",
        "Block A, 2nd floor",
        True,
    ),
    (
        "NEUR",
        "Neurology",
        "Brain, spine and nervous system consultation and diagnostics.",
        "202",
        "neurology@demohospital.com",
        "Block A, 3rd floor",
        True,
    ),
    (
        "ORTH",
        "Orthopaedics",
        "Bone, joint and sports injury care.",
        "203",
        "orthopaedics@demohospital.com",
        "Block B, 1st floor",
        True,
    ),
    (
        "PEDI",
        "Paediatrics",
        "Care for infants, children and adolescents up to 18.",
        "204",
        "paediatrics@demohospital.com",
        "Block B, 2nd floor",
        True,
    ),
    (
        "GENM",
        "General Medicine",
        "First-line consultation, chronic disease management and referrals.",
        "205",
        "generalmedicine@demohospital.com",
        "Block C, ground floor",
        True,
    ),
    (
        "DERM",
        "Dermatology",
        "Skin, hair and nail care. Currently closed for refurbishment.",
        "206",
        "dermatology@demohospital.com",
        "Block C, 1st floor",
        False,
    ),
]

#: Format: (email, first, last, phone, dept code, specialization, licence, fee,
#:          qualifications, languages, bio, availability, is_active)
DOCTORS: list[
    tuple[
        str,
        str,
        str,
        str,
        str,
        str,
        str,
        str,
        list[dict[str, Any]],
        list[str],
        str,
        list[tuple[int, time, time, int]],
        bool,
    ]
] = [
    (
        # The demo doctor login from seed.py §5 — given a clinical profile here
        # rather than a second account, so signing in as doctor@demohospital.com
        # lands on a doctor who actually has a schedule.
        "doctor@demohospital.com",
        "Priya",
        "Sharma",
        "+919812000101",
        "CARD",
        "Cardiology",
        "AP-MED-2011-4471",
        "800.00",
        [
            {"degree": "MBBS", "institution": "Osmania Medical College", "year": 2007},
            {"degree": "MD (General Medicine)", "institution": "AIIMS Delhi", "year": 2011},
            {"degree": "DM (Cardiology)", "institution": "AIIMS Delhi", "year": 2014},
        ],
        ["English", "Hindi", "Telugu"],
        "Interventional cardiologist with a focus on preventive care and post-operative follow-up.",
        _STANDARD_WEEK,
        True,
    ),
    (
        "arjun.nair@demohospital.com",
        "Arjun",
        "Nair",
        "+919812000102",
        "NEUR",
        "Neurology",
        "KA-MED-2014-8823",
        "950.00",
        [
            {"degree": "MBBS", "institution": "St John's Medical College", "year": 2010},
            {"degree": "MD (Medicine)", "institution": "NIMHANS", "year": 2014},
            {"degree": "DM (Neurology)", "institution": "NIMHANS", "year": 2017},
        ],
        ["English", "Malayalam", "Kannada"],
        "Neurologist treating epilepsy, migraine and movement disorders.",
        _STANDARD_WEEK,
        True,
    ),
    (
        "meera.krishnan@demohospital.com",
        "Meera",
        "Krishnan",
        "+919812000103",
        "PEDI",
        "Paediatrics",
        "TN-MED-2016-3390",
        "600.00",
        [
            {"degree": "MBBS", "institution": "Madras Medical College", "year": 2012},
            {"degree": "MD (Paediatrics)", "institution": "CMC Vellore", "year": 2016},
        ],
        ["English", "Tamil", "Hindi"],
        "Paediatrician with an interest in childhood nutrition and immunisation.",
        _MORNINGS_AND_SATURDAY,
        True,
    ),
    (
        "vikram.desai@demohospital.com",
        "Vikram",
        "Desai",
        "+919812000104",
        "ORTH",
        "Orthopaedics",
        "MH-MED-2009-1156",
        "1100.00",
        [
            {"degree": "MBBS", "institution": "Grant Medical College", "year": 2005},
            {"degree": "MS (Orthopaedics)", "institution": "KEM Hospital Mumbai", "year": 2009},
        ],
        ["English", "Hindi", "Marathi"],
        "Orthopaedic surgeon specialising in joint replacement and sports injuries.",
        _STANDARD_WEEK,
        True,
    ),
    (
        # Deactivated on purpose: gives the doctor list something to exclude, so
        # `include_inactive` is demonstrable without editing data by hand.
        "rohan.iyer@demohospital.com",
        "Rohan",
        "Iyer",
        "+919812000105",
        "GENM",
        "General Medicine",
        "KL-MED-2018-7712",
        "500.00",
        [{"degree": "MBBS", "institution": "Government Medical College Kozhikode", "year": 2014}],
        ["English", "Malayalam"],
        "General physician. Currently on extended sabbatical.",
        [],
        False,
    ),
]

#: Format: (first, last, years_old, birth_month, birth_day, gender, blood group,
#:          phone, email, city, allergies, chronic conditions, is_active)
#:
#: Ages are relative so the data does not age out of its own validation, and the
#: spread (3 to 82) plus the gender mix is what makes ``age_gte``/``age_lte`` and
#: ``gender`` filters worth showing.
PATIENTS: list[
    tuple[
        str,
        str,
        int,
        int,
        int,
        Gender,
        BloodGroup,
        str,
        str | None,
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        bool,
    ]
] = [
    (
        "Ananya",
        "Rao",
        38,
        3,
        14,
        Gender.FEMALE,
        BloodGroup.B_POSITIVE,
        "+919812345601",
        "ananya.rao@example.com",
        "Hyderabad",
        [{"name": "Penicillin", "severity": "severe", "reaction": "Anaphylaxis"}],
        [{"name": "Hypothyroidism", "since_year": 2019}],
        True,
    ),
    (
        "Ravi",
        "Menon",
        54,
        7,
        2,
        Gender.MALE,
        BloodGroup.O_POSITIVE,
        "+919812345602",
        "ravi.menon@example.com",
        "Bengaluru",
        [],
        [
            {"name": "Type 2 Diabetes", "since_year": 2015},
            {"name": "Hypertension", "since_year": 2018},
        ],
        True,
    ),
    (
        "Fatima",
        "Sheikh",
        27,
        11,
        23,
        Gender.FEMALE,
        BloodGroup.A_NEGATIVE,
        "+919812345603",
        "fatima.sheikh@example.com",
        "Bengaluru",
        [{"name": "Dust mites", "severity": "moderate", "reaction": "Rhinitis"}],
        [{"name": "Asthma", "since_year": 2011}],
        True,
    ),
    (
        "Thomas",
        "George",
        61,
        1,
        9,
        Gender.MALE,
        BloodGroup.AB_POSITIVE,
        "+919812345604",
        "thomas.george@example.com",
        "Kochi",
        [{"name": "Sulfa drugs", "severity": "mild", "reaction": "Rash"}],
        [{"name": "Coronary artery disease", "since_year": 2021}],
        True,
    ),
    (
        "Meera",
        "Nair",
        45,
        5,
        30,
        Gender.FEMALE,
        BloodGroup.O_NEGATIVE,
        "+919812345605",
        "meera.nair@example.com",
        "Thiruvananthapuram",
        [],
        [],
        True,
    ),
    (
        "Ishaan",
        "Kulkarni",
        8,
        9,
        12,
        Gender.MALE,
        BloodGroup.A_POSITIVE,
        "+919812345606",
        None,
        "Pune",
        [{"name": "Peanuts", "severity": "severe", "reaction": "Swelling, breathlessness"}],
        [],
        True,
    ),
    (
        "Aarav",
        "Sen",
        3,
        2,
        18,
        Gender.MALE,
        BloodGroup.B_NEGATIVE,
        "+919812345607",
        None,
        "Kolkata",
        [],
        [],
        True,
    ),
    (
        "Devi",
        "Lakshmi",
        82,
        8,
        5,
        Gender.FEMALE,
        BloodGroup.O_POSITIVE,
        "+919812345608",
        None,
        "Madurai",
        [{"name": "Aspirin", "severity": "moderate", "reaction": "Gastric irritation"}],
        [
            {"name": "Osteoarthritis", "since_year": 2006},
            {"name": "Hypertension", "since_year": 2010},
        ],
        True,
    ),
    (
        "Kabir",
        "Malhotra",
        39,
        4,
        21,
        Gender.MALE,
        BloodGroup.A_POSITIVE,
        "+919812345609",
        "kabir.malhotra@example.com",
        "Delhi",
        [],
        [{"name": "Migraine", "since_year": 2016}],
        True,
    ),
    (
        # `other` and `unspecified` both appear so the frontend renders the full
        # gender enum rather than assuming a binary.
        "Riya",
        "Fernandes",
        31,
        12,
        3,
        Gender.OTHER,
        BloodGroup.AB_NEGATIVE,
        "+919812345610",
        "riya.fernandes@example.com",
        "Goa",
        [],
        [],
        True,
    ),
    (
        "Sam",
        "Varghese",
        24,
        6,
        15,
        Gender.UNSPECIFIED,
        BloodGroup.O_POSITIVE,
        "+919812345611",
        "sam.varghese@example.com",
        "Kochi",
        [{"name": "Latex", "severity": "mild", "reaction": "Contact dermatitis"}],
        [],
        True,
    ),
    (
        # Deactivated: proves `include_inactive` and that a soft-deleted patient
        # keeps its appointment history.
        "Harish",
        "Pillai",
        67,
        10,
        27,
        Gender.MALE,
        BloodGroup.B_POSITIVE,
        "+919812345612",
        None,
        "Chennai",
        [],
        [{"name": "Chronic kidney disease", "since_year": 2020}],
        False,
    ),
]


def _clinic_datetime(day_offset: int, at: time, *, today: date) -> datetime:
    """Return a UTC instant for a clinic wall-clock time on a relative day.

    :param day_offset: Days from ``today``; negative is in the past.
    :param at: Wall-clock time in the clinic's timezone.
    :param today: The date the seed treats as today.
    :returns: The equivalent timezone-aware instant, converted to UTC.
    """
    local = datetime.combine(today + timedelta(days=day_offset), at, tzinfo=CLINIC_TZ)
    return local.astimezone(UTC)


def _weekday_offsets(today: date, *, count: int, backwards: bool = False) -> list[int]:
    """Return day offsets from ``today`` that land on Monday–Friday.

    Seeding on a Friday would otherwise put "tomorrow's" bookings on a Saturday,
    when no doctor publishes availability — the appointment would exist but the
    slots read model would never show it as ``booked``, which is exactly the
    join the frontend needs to see working.

    :param today: The date the seed treats as today.
    :param count: How many working days to return.
    :param backwards: Search into the past instead of the future.
    :returns: Offsets in the order encountered, nearest first.
    """
    step = -1 if backwards else 1
    offsets: list[int] = []
    day = step
    while len(offsets) < count:
        if (today + timedelta(days=day)).weekday() < 5:  # noqa: PLR2004 — Sat/Sun are 5 and 6.
            offsets.append(day)
        day += step
    return offsets


def _birth_date(years_old: int, month: int, day: int, *, today: date) -> date:
    """Return a date of birth that makes someone ``years_old`` today.

    Computed rather than hard-coded so the fixtures do not silently age past
    the paediatric or geriatric bands they were chosen to sit in.

    :param years_old: Desired age in completed years.
    :param month: Birth month.
    :param day: Birth day of month.
    :param today: The date the seed treats as today.
    :returns: The date of birth.
    """
    year = today.year - years_old
    if (month, day) > (today.month, today.day):
        # The birthday has not come round yet this year, so a naive subtraction
        # would leave them a year younger than intended.
        year -= 1
    return date(year, month, day)


async def _seed_departments(session: AsyncSession, hospital: Hospital) -> dict[str, Department]:
    """Create the department catalogue if it is not already there.

    :param session: The open session; the caller commits.
    :param hospital: The tenant to attach departments to.
    :returns: Departments by code.
    """
    departments: dict[str, Department] = {}
    created = 0

    for code, name, description, extension, email, location, is_active in DEPARTMENTS:
        stmt = select(Department).where(
            Department.hospital_id == hospital.id, Department.code == code
        )
        result = await session.execute(stmt)
        department = result.unique().scalar_one_or_none()

        if department is None:
            department = Department(
                hospital_id=hospital.id,
                code=code,
                name=name,
                description=description,
                phone_extension=extension,
                email=email,
                location=location,
                deleted_at=None if is_active else datetime.now(UTC),
            )
            session.add(department)
            await session.flush()
            created += 1

        departments[code] = department

    logger.info("demo_departments_seeded", total=len(departments), created=created)
    return departments


async def _seed_doctor_user(
    session: AsyncSession,
    hospital: Hospital,
    doctor_role: Role | None,
    *,
    email: str,
    first_name: str,
    last_name: str,
    phone: str,
) -> User:
    """Find or create the user record a doctor profile hangs off.

    A doctor is a profile attached to a user (module spec §5.1), so the user has
    to exist first. Idempotent on ``(hospital_id, email)``.

    :param session: The open session.
    :param hospital: The tenant.
    :param doctor_role: The seeded Doctor role, assigned on creation.
    :param email: Login address, the natural key.
    :param first_name: Given name.
    :param last_name: Family name.
    :param phone: Contact number in E.164 form.
    :returns: The existing or newly created user.
    """
    stmt = select(User).where(User.hospital_id == hospital.id, User.email == email)
    result = await session.execute(stmt)
    user = result.unique().scalar_one_or_none()

    if user is not None:
        return user

    user = User(
        hospital_id=hospital.id,
        email=email,
        phone=phone,
        password_hash=hash_password(DEMO_PASSWORD),
        first_name=first_name,
        last_name=last_name,
        status=UserStatus.ACTIVE,
        password_changed_at=datetime.now(UTC),
    )
    session.add(user)
    await session.flush()

    if doctor_role is not None:
        session.add(UserRole(user_id=user.id, role_id=doctor_role.id))

    logger.debug("demo_doctor_user_created", email=email)
    return user


async def _seed_doctors(
    session: AsyncSession,
    hospital: Hospital,
    departments: dict[str, Department],
    doctor_role: Role | None,
    *,
    today: date,
) -> dict[str, Doctor]:
    """Create doctor profiles, their weekly availability, and one leave block.

    :param session: The open session.
    :param hospital: The tenant.
    :param departments: Departments by code, to resolve ``department_id``.
    :param doctor_role: Role assigned to newly created doctor users.
    :param today: The date the seed treats as today, for the leave window.
    :returns: Doctors by licence number.
    """
    doctors: dict[str, Doctor] = {}
    created = 0

    for entry in DOCTORS:
        (
            email,
            first_name,
            last_name,
            phone,
            dept_code,
            specialization,
            license_number,
            fee,
            qualifications,
            languages,
            bio,
            availability,
            is_active,
        ) = entry

        stmt = select(Doctor).where(
            Doctor.hospital_id == hospital.id, Doctor.license_number == license_number
        )
        result = await session.execute(stmt)
        doctor = result.unique().scalar_one_or_none()

        if doctor is None:
            user = await _seed_doctor_user(
                session,
                hospital,
                doctor_role,
                email=email,
                first_name=first_name,
                last_name=last_name,
                phone=phone,
            )
            department = departments.get(dept_code)
            doctor = Doctor(
                hospital_id=hospital.id,
                user_id=user.id,
                department_id=department.id if department else None,
                specialization=specialization,
                license_number=license_number,
                consultation_fee=Decimal(fee),
                qualifications=qualifications,
                languages=languages,
                bio=bio,
                deleted_at=None if is_active else datetime.now(UTC),
            )
            session.add(doctor)
            await session.flush()
            created += 1

        doctors[license_number] = doctor

        await _seed_availability(session, hospital, doctor, availability)

    # One doctor is away for the next two working days, so the slots read model
    # has `on_leave` slots to return and the leave endpoints have something to
    # list. Working days, not calendar days, or the leave can fall entirely on a
    # weekend the doctor was not working anyway.
    away = doctors.get("KA-MED-2014-8823")
    if away is not None:
        first_working_day = _weekday_offsets(today, count=1)[0]
        await _seed_leave(
            session,
            hospital,
            away,
            starts_at=_clinic_datetime(first_working_day, time(0, 0), today=today),
            ends_at=_clinic_datetime(first_working_day + 2, time(0, 0), today=today),
            reason="Conference — annual neurology update",
        )

    logger.info("demo_doctors_seeded", total=len(doctors), created=created)
    return doctors


async def _seed_availability(
    session: AsyncSession,
    hospital: Hospital,
    doctor: Doctor,
    windows: list[tuple[int, time, time, int]],
) -> None:
    """Give a doctor a weekly schedule, unless they already have one.

    Availability is replaced whole by the API (module spec §5.2), so the natural
    idempotency check is "does this doctor have any window at all" rather than a
    per-row comparison — that way a schedule edited by hand after seeding is not
    silently overwritten on the next run.

    :param session: The open session.
    :param hospital: The tenant.
    :param doctor: The doctor to schedule.
    :param windows: ``(day_of_week, start, end, slot_minutes)`` tuples.
    """
    if not windows:
        return

    existing = await session.execute(
        select(DoctorAvailability.id).where(DoctorAvailability.doctor_id == doctor.id).limit(1)
    )
    if existing.scalar_one_or_none() is not None:
        return

    for day_of_week, start_time, end_time, slot_minutes in windows:
        session.add(
            DoctorAvailability(
                hospital_id=hospital.id,
                doctor_id=doctor.id,
                day_of_week=day_of_week,
                start_time=start_time,
                end_time=end_time,
                slot_duration_minutes=slot_minutes,
            )
        )
    await session.flush()


async def _seed_leave(
    session: AsyncSession,
    hospital: Hospital,
    doctor: Doctor,
    *,
    starts_at: datetime,
    ends_at: datetime,
    reason: str,
) -> None:
    """Record one leave block, idempotent on ``(doctor_id, starts_at)``.

    :param session: The open session.
    :param hospital: The tenant.
    :param doctor: The doctor going on leave.
    :param starts_at: Inclusive start.
    :param ends_at: Exclusive end.
    :param reason: Why they are away.
    """
    stmt = select(DoctorLeave).where(
        DoctorLeave.doctor_id == doctor.id, DoctorLeave.starts_at == starts_at
    )
    result = await session.execute(stmt)
    if result.unique().scalar_one_or_none() is not None:
        return

    session.add(
        DoctorLeave(
            hospital_id=hospital.id,
            doctor_id=doctor.id,
            starts_at=starts_at,
            ends_at=ends_at,
            reason=reason,
        )
    )
    await session.flush()


async def _seed_patients(
    session: AsyncSession, hospital: Hospital, *, today: date
) -> dict[str, Patient]:
    """Register the demo patients, allocating a real MRN for each new one.

    MRNs come from :class:`~app.services.mrn_service.MRNService` rather than
    being written by hand, so the ``mrn_sequences`` counter stays consistent with
    the rows in ``patients`` and the next patient registered through the API
    continues the same series.

    :param session: The open session; the counter lock is held until it commits.
    :param hospital: The tenant.
    :param today: The date the seed treats as today, for age arithmetic.
    :returns: Patients by ``"first last"``.
    """
    mrn_service = MRNService(MrnSequenceRepository(session))
    patients: dict[str, Patient] = {}
    created = 0

    for entry in PATIENTS:
        (
            first_name,
            last_name,
            years_old,
            birth_month,
            birth_day,
            gender,
            blood_group,
            phone,
            email,
            city,
            allergies,
            chronic_conditions,
            is_active,
        ) = entry

        date_of_birth = _birth_date(years_old, birth_month, birth_day, today=today)
        stmt = select(Patient).where(
            Patient.hospital_id == hospital.id,
            Patient.first_name == first_name,
            Patient.last_name == last_name,
            Patient.date_of_birth == date_of_birth,
        )
        result = await session.execute(stmt)
        patient = result.unique().scalar_one_or_none()

        if patient is None:
            patient = Patient(
                hospital_id=hospital.id,
                mrn=await mrn_service.next(hospital.id),
                first_name=first_name,
                last_name=last_name,
                date_of_birth=date_of_birth,
                gender=gender,
                blood_group=blood_group.value,
                phone=phone,
                email=email,
                address={"line1": f"{city} residence", "city": city, "country": "IN"},
                emergency_contact={
                    "name": f"{last_name} family contact",
                    "phone": "+919800000000",
                    "relation": "Family",
                },
                allergies=allergies,
                chronic_conditions=chronic_conditions,
                current_medications=[],
                deleted_at=None if is_active else datetime.now(UTC),
            )
            session.add(patient)
            await session.flush()
            created += 1

        patients[f"{first_name} {last_name}"] = patient

    logger.info("demo_patients_seeded", total=len(patients), created=created)
    return patients


async def _seed_appointments(
    session: AsyncSession,
    hospital: Hospital,
    doctors: dict[str, Doctor],
    patients: dict[str, Patient],
    *,
    today: date,
    actor_id: uuid.UUID | None,
) -> None:
    """Book appointments covering every status, with their status history.

    Each row carries a fixed ``idempotency_key``, which is the same mechanism the
    booking API uses to make a client retry safe (business rule 8) and the reason
    a second seed run books nothing.

    The layout keeps every live appointment for a given doctor at a distinct
    time: the ``no_overlap_per_doctor`` exclusion constraint would reject the
    transaction otherwise, and working around it by pre-cancelling rows would
    make the demo data a worse example than the constraint it dodged.

    :param session: The open session.
    :param hospital: The tenant.
    :param doctors: Doctors by licence number.
    :param patients: Patients by full name.
    :param today: The date the seed treats as today.
    :param actor_id: User recorded against each status transition.
    """
    # Weekday anchors. Working days rather than raw ±1/±2, so a seed run on a
    # Friday or a Sunday still produces a demo where the finished appointments
    # are on a day the clinic was open and the future ones fall on days the
    # doctors publish slots for.
    past = _weekday_offsets(today, count=1, backwards=True)[0]
    future = _weekday_offsets(today, count=2)

    cardiologist = doctors["AP-MED-2011-4471"]
    neurologist = doctors["KA-MED-2014-8823"]
    paediatrician = doctors["TN-MED-2016-3390"]
    orthopaedist = doctors["MH-MED-2009-1156"]

    # (key, doctor, patient, day offset, start, minutes, status, type, reason)
    plan: list[
        tuple[str, Doctor, Patient, int, time, int, AppointmentStatus, AppointmentType, str]
    ] = [
        # The last working day — the finished states.
        (
            "seed-appt-0001",
            cardiologist,
            patients["Ravi Menon"],
            past,
            time(9, 0),
            30,
            AppointmentStatus.COMPLETED,
            AppointmentType.FOLLOW_UP,
            "Diabetes and blood pressure review",
        ),
        (
            "seed-appt-0002",
            cardiologist,
            patients["Thomas George"],
            past,
            time(9, 30),
            30,
            AppointmentStatus.COMPLETED,
            AppointmentType.FOLLOW_UP,
            "Post-angioplasty follow-up",
        ),
        (
            "seed-appt-0003",
            paediatrician,
            patients["Ishaan Kulkarni"],
            past,
            time(9, 20),
            20,
            AppointmentStatus.COMPLETED,
            AppointmentType.NEW,
            "Recurrent cough",
        ),
        (
            "seed-appt-0004",
            neurologist,
            patients["Kabir Malhotra"],
            past,
            time(10, 0),
            30,
            AppointmentStatus.CANCELLED,
            AppointmentType.NEW,
            "Migraine assessment",
        ),
        (
            "seed-appt-0005",
            orthopaedist,
            patients["Devi Lakshmi"],
            past,
            time(11, 0),
            30,
            AppointmentStatus.NO_SHOW,
            AppointmentType.FOLLOW_UP,
            "Knee osteoarthritis review",
        ),
        # Today — the in-flight states, including the walk-in queue.
        (
            "seed-appt-0006",
            cardiologist,
            patients["Ananya Rao"],
            0,
            time(10, 0),
            30,
            AppointmentStatus.IN_PROGRESS,
            AppointmentType.NEW,
            "Palpitations and breathlessness",
        ),
        (
            "seed-appt-0007",
            cardiologist,
            patients["Harish Pillai"],
            0,
            time(11, 0),
            30,
            AppointmentStatus.CHECKED_IN,
            AppointmentType.FOLLOW_UP,
            "Renal function and blood pressure review",
        ),
        (
            "seed-appt-0008",
            orthopaedist,
            patients["Sam Varghese"],
            0,
            time(11, 30),
            30,
            AppointmentStatus.CHECKED_IN,
            AppointmentType.WALK_IN,
            "Ankle injury after a fall",
        ),
        (
            "seed-appt-0009",
            paediatrician,
            patients["Aarav Sen"],
            0,
            time(10, 20),
            20,
            AppointmentStatus.CHECKED_IN,
            AppointmentType.WALK_IN,
            "Fever since last night",
        ),
        (
            "seed-appt-0010",
            orthopaedist,
            patients["Meera Nair"],
            0,
            time(15, 0),
            30,
            AppointmentStatus.BOOKED,
            AppointmentType.NEW,
            "Lower back pain",
        ),
        # The next two working days — future bookings.
        (
            "seed-appt-0011",
            cardiologist,
            patients["Fatima Sheikh"],
            future[0],
            time(9, 30),
            30,
            AppointmentStatus.BOOKED,
            AppointmentType.NEW,
            "Chest tightness during exercise",
        ),
        (
            "seed-appt-0012",
            paediatrician,
            patients["Ishaan Kulkarni"],
            future[0],
            time(9, 0),
            20,
            AppointmentStatus.BOOKED,
            AppointmentType.FOLLOW_UP,
            "Review after treatment for cough",
        ),
        (
            "seed-appt-0013",
            orthopaedist,
            patients["Riya Fernandes"],
            future[1],
            time(10, 0),
            30,
            AppointmentStatus.BOOKED,
            AppointmentType.NEW,
            "Shoulder stiffness",
        ),
        (
            "seed-appt-0014",
            cardiologist,
            patients["Devi Lakshmi"],
            future[1],
            time(11, 30),
            30,
            AppointmentStatus.BOOKED,
            AppointmentType.EMERGENCY,
            "Sudden severe chest pain",
        ),
    ]

    created = 0
    for key, doctor, patient, day_offset, start, minutes, status, kind, reason in plan:
        stmt = select(Appointment).where(
            Appointment.hospital_id == hospital.id, Appointment.idempotency_key == key
        )
        result = await session.execute(stmt)
        if result.unique().scalar_one_or_none() is not None:
            continue

        scheduled_start = _clinic_datetime(day_offset, start, today=today)
        scheduled_end = scheduled_start + timedelta(minutes=minutes)

        appointment = Appointment(
            hospital_id=hospital.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_start=scheduled_start,
            scheduled_end=scheduled_end,
            status=status,
            type=kind,
            reason=reason,
            cancelled_reason=(
                "Patient rescheduling to next month."
                if status is AppointmentStatus.CANCELLED
                else None
            ),
            checked_in_at=(
                scheduled_start - timedelta(minutes=10)
                if status
                in {
                    AppointmentStatus.CHECKED_IN,
                    AppointmentStatus.IN_PROGRESS,
                    AppointmentStatus.COMPLETED,
                }
                else None
            ),
            started_at=(
                scheduled_start
                if status in {AppointmentStatus.IN_PROGRESS, AppointmentStatus.COMPLETED}
                else None
            ),
            completed_at=(scheduled_end if status is AppointmentStatus.COMPLETED else None),
            idempotency_key=key,
            created_by=actor_id,
        )
        session.add(appointment)
        await session.flush()

        _add_status_history(session, appointment, actor_id=actor_id)
        created += 1

    await session.flush()
    logger.info("demo_appointments_seeded", total=len(plan), created=created)


#: The transitions each terminal status was reached through. Business rule 7
#: requires one history row per change, so seeded appointments carry the trail
#: they would have if they had been driven through the API.
_STATUS_PATHS: dict[AppointmentStatus, list[AppointmentStatus]] = {
    AppointmentStatus.BOOKED: [AppointmentStatus.BOOKED],
    AppointmentStatus.CHECKED_IN: [AppointmentStatus.BOOKED, AppointmentStatus.CHECKED_IN],
    AppointmentStatus.IN_PROGRESS: [
        AppointmentStatus.BOOKED,
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
    ],
    AppointmentStatus.COMPLETED: [
        AppointmentStatus.BOOKED,
        AppointmentStatus.CHECKED_IN,
        AppointmentStatus.IN_PROGRESS,
        AppointmentStatus.COMPLETED,
    ],
    AppointmentStatus.CANCELLED: [AppointmentStatus.BOOKED, AppointmentStatus.CANCELLED],
    AppointmentStatus.NO_SHOW: [AppointmentStatus.BOOKED, AppointmentStatus.NO_SHOW],
}


def _add_status_history(
    session: AsyncSession, appointment: Appointment, *, actor_id: uuid.UUID | None
) -> None:
    """Append the transition trail that led to an appointment's status.

    :param session: The open session.
    :param appointment: The freshly created appointment.
    :param actor_id: User recorded as having made each change.
    """
    path = _STATUS_PATHS[appointment.status]
    previous: AppointmentStatus | None = None
    # Spread the changes over the run-up to the appointment so the trail reads
    # in order rather than sharing one timestamp.
    changed_at = appointment.scheduled_start - timedelta(hours=len(path))

    for index, to_status in enumerate(path):
        session.add(
            AppointmentStatusHistory(
                hospital_id=appointment.hospital_id,
                appointment_id=appointment.id,
                from_status=previous,
                to_status=to_status,
                changed_by=actor_id,
                changed_at=changed_at + timedelta(hours=index),
                reason=appointment.cancelled_reason
                if to_status is AppointmentStatus.CANCELLED
                else None,
            )
        )
        previous = to_status


async def seed_demo_data(
    session: AsyncSession,
    hospital: Hospital,
    role_map: dict[str, Role],
    *,
    actor_id: uuid.UUID | None = None,
) -> None:
    """Seed departments, doctors, patients and appointments for the demo hospital.

    Safe to run repeatedly: see the module docstring for the natural key used
    per entity. The caller owns the transaction and commits.

    :param session: An open session inside a transaction.
    :param hospital: The demo hospital every row is scoped to.
    :param role_map: Seeded system roles by name, for the Doctor role.
    :param actor_id: User recorded as the author of seeded appointments.
    """
    today = datetime.now(UTC).astimezone(CLINIC_TZ).date()

    departments = await _seed_departments(session, hospital)
    doctors = await _seed_doctors(
        session, hospital, departments, role_map.get("Doctor"), today=today
    )
    patients = await _seed_patients(session, hospital, today=today)
    await _seed_appointments(session, hospital, doctors, patients, today=today, actor_id=actor_id)

    logger.info("demo_data_seeded")
