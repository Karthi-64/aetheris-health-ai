"""Frontend ↔ backend permission parity (20-day plan, Days 14–17).

The SPA decides what to render from the permission codes the server sends; the
server decides what a request may do from the same strings. If the two
vocabularies drift — a code renamed on one side only — the UI silently shows
actions that always fail, or hides ones the user is entitled to, and no
per-module test catches it because each side is internally consistent.

This test parses the ``Permission`` union in ``frontend/src/lib/rbac.ts`` and
compares it with the seeded catalog in ``app/seeds/seed.py``. It skips when
the frontend is not checked out (backend-only CI). ``dashboard.view`` is the
one client-only pseudo-code: the backend catalog has no such permission
because the Dashboard is the home page and any authenticated user with at
least one permission may see it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.seeds.seed import PERMISSION_DEFINITIONS

#: Client-only nav pseudo-code — deliberately absent from the server catalog.
CLIENT_ONLY_PERMISSIONS = frozenset({"dashboard.view"})

_BACKEND_RBAC = Path(__file__).resolve().parents[4] / "frontend" / "src" / "lib" / "rbac.ts"

pytestmark = pytest.mark.skipif(
    not _BACKEND_RBAC.exists(),
    reason="frontend/src/lib/rbac.ts not present in this checkout",
)


def _backend_catalog() -> set[str]:
    """Permission codes the seed script issues."""
    return {code for code, _module, _description in PERMISSION_DEFINITIONS}


def _frontend_permissions() -> set[str]:
    """Permission codes named in the client's ``Permission`` union."""
    text = _BACKEND_RBAC.read_text(encoding="utf-8")
    match = re.search(r"export type Permission =(?P<body>.*?)(?:\n\n|\n/\*\*)", text, re.S)
    assert match is not None, "Could not locate `export type Permission =` in rbac.ts"
    return set(re.findall(r"'([a-z][a-z0-9_.]*)'", match.group("body")))


def test_frontend_union_parses_to_a_meaningful_set() -> None:
    """Guard against the parser silently matching nothing."""
    frontend = _frontend_permissions()
    assert len(frontend) >= 20, f"Only parsed {len(frontend)} codes — parser or union broke"


def test_every_backend_code_is_known_to_the_frontend() -> None:
    """A code the server can send must exist in the client's union.

    Otherwise ``hasPermission``/``RequirePermission`` can never grant it and
    the UI hides functionality the user actually holds.
    """
    missing = _backend_catalog() - _frontend_permissions()
    assert not missing, f"Backend permissions missing from frontend union: {sorted(missing)}"


def test_frontend_invents_no_unseeded_codes() -> None:
    """A code the client checks must exist in the seeded catalog.

    Otherwise the check can never pass — a permanently dead branch. The only
    exception is the documented client-only pseudo-code.
    """
    unknown = _frontend_permissions() - _backend_catalog() - CLIENT_ONLY_PERMISSIONS
    assert not unknown, f"Frontend checks codes the backend never issues: {sorted(unknown)}"


def test_nav_gate_codes_are_all_seeded() -> None:
    """Every permission the sidebar gates on must be a real server code."""
    text = _BACKEND_RBAC.read_text(encoding="utf-8")
    nav_block = re.search(r"export const NAV: NavItem\[\] = \[(.*?)\n\]", text, re.S)
    assert nav_block is not None, "NAV table not found in rbac.ts"
    gate_codes = set(re.findall(r"permission: '([a-z][a-z0-9_.]*)'", nav_block.group(1)))
    assert gate_codes, "No nav gates parsed"
    unknown = gate_codes - _backend_catalog() - CLIENT_ONLY_PERMISSIONS
    assert not unknown, f"Nav gated on unseeded codes: {sorted(unknown)}"
