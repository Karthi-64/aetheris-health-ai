"""Patient-facing DTOs (``docs/modules/15-patient-app.md`` §6 and §27).

Every response a patient endpoint returns is built from a schema in this
package: an explicit allow-list of fields, never a staff response schema. Every
request schema forbids unknown fields, so a ``patient_id``, a ``phone`` or a
``hospital_id`` slipped into a body is rejected rather than silently ignored.
"""
