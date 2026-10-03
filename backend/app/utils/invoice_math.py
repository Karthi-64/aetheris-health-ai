"""Invoice arithmetic — pure functions, ``Decimal`` only, no I/O.

Implements the money rules in ``docs/modules/06-billing.md``:

- §4 rule 8: a line is ``unit_price * quantity`` plus tax on that amount.
- §4 rule 7: an invoice total is ``subtotal + tax_amount - discount_amount``.
- §14: round **half to even**, per line, then sum.

Rounding per line and then summing — rather than summing and rounding once — is
what makes the printed lines add up to the printed total (AC-5, AC-7). The
price is a fraction of a paisa either way; an invoice whose lines do not sum to
its total is a complaint.

``float`` is refused outright rather than converted. A float that reaches this
module is already wrong (``0.1 + 0.2``), and quietly accepting one would hide
the bug at the only place positioned to catch it (CLAUDE.md rule 6).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "MONEY_QUANTUM",
    "ZERO",
    "InvoiceTotals",
    "LineAmounts",
    "compute_invoice_totals",
    "compute_line",
    "round_money",
]

#: Two decimal places — the paisa, the INR minor unit (AC-7).
MONEY_QUANTUM = Decimal("0.01")

#: Zero as money, so callers never write a bare literal.
ZERO = Decimal("0.00")

_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class LineAmounts:
    """The three amounts one invoice line resolves to.

    :param net: ``unit_price * quantity``, before tax.
    :param tax: Tax on ``net``.
    :param total: ``net + tax`` — what is stored as ``line_total``.
    """

    net: Decimal
    tax: Decimal
    total: Decimal


@dataclass(frozen=True, slots=True)
class InvoiceTotals:
    """The amounts stored on an invoice.

    :param subtotal: Sum of line ``net`` amounts.
    :param tax_amount: Sum of line ``tax`` amounts.
    :param discount_amount: Invoice-level discount.
    :param total: ``subtotal + tax_amount - discount_amount``.
    """

    subtotal: Decimal
    tax_amount: Decimal
    discount_amount: Decimal
    total: Decimal


def _require_decimal(value: object, name: str) -> Decimal:
    """Return ``value`` if it is a :class:`Decimal`, and refuse anything else.

    :param value: The value to check.
    :param name: Parameter name, for the message.
    :returns: The value unchanged.
    :raises TypeError: If the value is not a ``Decimal`` — in particular a float.
    """
    if not isinstance(value, Decimal):
        msg = f"{name} must be a Decimal, got {type(value).__name__}."
        raise TypeError(msg)
    return value


def round_money(value: Decimal) -> Decimal:
    """Round to two decimal places, half to even (module spec §14).

    :param value: The amount to round.
    :returns: The amount quantized to :data:`MONEY_QUANTUM`.
    :raises TypeError: If ``value`` is not a ``Decimal``.
    """
    return _require_decimal(value, "value").quantize(MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def compute_line(unit_price: Decimal, quantity: Decimal, tax_rate: Decimal) -> LineAmounts:
    """Resolve one invoice line (business rule 8).

    :param unit_price: Price per unit. Must not be negative.
    :param quantity: Units billed. Must be positive.
    :param tax_rate: Tax as a percentage, 0 to 100 — ``Decimal("18")`` is 18%.
    :returns: The line's net, tax and total, each rounded to the paisa.
    :raises TypeError: If any argument is not a ``Decimal``.
    :raises ValueError: If an argument is outside its allowed range.
    """
    _require_decimal(unit_price, "unit_price")
    _require_decimal(quantity, "quantity")
    _require_decimal(tax_rate, "tax_rate")

    if unit_price < 0:
        msg = "unit_price must not be negative."
        raise ValueError(msg)
    if quantity <= 0:
        msg = "quantity must be greater than zero."
        raise ValueError(msg)
    if not 0 <= tax_rate <= _HUNDRED:
        msg = "tax_rate must be between 0 and 100."
        raise ValueError(msg)

    net = round_money(unit_price * quantity)
    tax = round_money(net * tax_rate / _HUNDRED)
    return LineAmounts(net=net, tax=tax, total=net + tax)


def compute_invoice_totals(
    lines: Iterable[LineAmounts], *, discount_amount: Decimal = ZERO
) -> InvoiceTotals:
    """Sum resolved lines into invoice totals (business rule 7).

    :param lines: The invoice's lines, already resolved by :func:`compute_line`.
    :param discount_amount: Invoice-level discount. Must not exceed the subtotal.
    :returns: Subtotal, tax, discount and total.
    :raises TypeError: If ``discount_amount`` is not a ``Decimal``.
    :raises ValueError: If the discount is negative or exceeds the subtotal.
    """
    _require_decimal(discount_amount, "discount_amount")

    subtotal = ZERO
    tax_amount = ZERO
    for line in lines:
        subtotal += line.net
        tax_amount += line.tax

    discount = round_money(discount_amount)
    if discount < 0:
        msg = "discount_amount must not be negative."
        raise ValueError(msg)
    if discount > subtotal:
        msg = "discount_amount must not exceed the subtotal."
        raise ValueError(msg)

    return InvoiceTotals(
        subtotal=subtotal,
        tax_amount=tax_amount,
        discount_amount=discount,
        total=subtotal + tax_amount - discount,
    )
