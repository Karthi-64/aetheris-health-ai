"""Tests for :mod:`app.utils.invoice_math`.

``docs/modules/06-billing.md`` §16 asks for the money math to be fuzzed, and
AC-7 for it to be accurate to the paisa. The example tests pin the rules the
spec states (§4 rules 7 and 8, §14 rounding); the property tests then check
those rules hold for inputs nobody thought to write down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.utils.invoice_math import (
    MONEY_QUANTUM,
    ZERO,
    LineAmounts,
    compute_invoice_totals,
    compute_line,
    round_money,
)

# ``database=None`` keeps hypothesis from writing an example cache into the
# working tree; ``deadline=None`` because a first-call import can blow the
# default 200ms on a cold CI runner and fail a test that is not slow.
PROPERTY = settings(database=None, deadline=None, max_examples=300)

#: Prices and quantities as the schemas admit them: 2 decimal places, bounded.
prices = st.decimals(min_value=Decimal(0), max_value=Decimal("999999.99"), places=2)
quantities = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("9999.99"), places=2)
tax_rates = st.decimals(min_value=Decimal(0), max_value=Decimal(100), places=2)
line_inputs = st.tuples(prices, quantities, tax_rates)


def _places(value: Decimal) -> int:
    """Return how many decimal places a Decimal carries."""
    exponent = value.as_tuple().exponent
    assert isinstance(exponent, int)
    return -exponent


class TestRoundMoney:
    """Half-to-even rounding (module spec §14)."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # The four cases that distinguish half-even from half-up.
            ("0.125", "0.12"),
            ("0.135", "0.14"),
            ("2.675", "2.68"),
            ("2.665", "2.66"),
            # Not a tie: rounds to nearest either way.
            ("0.1251", "0.13"),
            ("0.1249", "0.12"),
            ("10", "10.00"),
        ],
    )
    def test_rounds_half_to_even(self, raw: str, expected: str) -> None:
        assert round_money(Decimal(raw)) == Decimal(expected)

    def test_result_always_has_two_places(self) -> None:
        assert _places(round_money(Decimal(7))) == 2

    def test_refuses_a_float(self) -> None:
        with pytest.raises(TypeError, match="must be a Decimal"):
            round_money(0.1)  # type: ignore[arg-type]


class TestComputeLine:
    """One invoice line (business rule 8)."""

    def test_untaxed_line(self) -> None:
        line = compute_line(Decimal("75.00"), Decimal(2), ZERO)

        assert line == LineAmounts(
            net=Decimal("150.00"), tax=Decimal("0.00"), total=Decimal("150.00")
        )

    def test_taxed_line(self) -> None:
        # 1200.00 at 18% -> 216.00 tax.
        line = compute_line(Decimal("1200.00"), Decimal(1), Decimal(18))

        assert line.net == Decimal("1200.00")
        assert line.tax == Decimal("216.00")
        assert line.total == Decimal("1416.00")

    def test_fractional_quantity(self) -> None:
        # 2.5 units at 99.99 = 249.975, a tie that rounds to the even 249.98.
        line = compute_line(Decimal("99.99"), Decimal("2.5"), ZERO)

        assert line.net == Decimal("249.98")

    def test_tax_is_taken_on_the_rounded_net(self) -> None:
        # net 0.33 * 3 = 0.99; 5% of 0.99 = 0.0495 -> 0.05.
        line = compute_line(Decimal("0.33"), Decimal(3), Decimal(5))

        assert line.tax == Decimal("0.05")
        assert line.total == Decimal("1.04")

    def test_free_line_is_allowed(self) -> None:
        assert compute_line(ZERO, Decimal(1), Decimal(18)).total == Decimal("0.00")

    @pytest.mark.parametrize(
        ("unit_price", "quantity", "tax_rate", "message"),
        [
            ("-0.01", "1", "0", "unit_price must not be negative"),
            ("1", "0", "0", "quantity must be greater than zero"),
            ("1", "-1", "0", "quantity must be greater than zero"),
            ("1", "1", "-0.01", "tax_rate must be between 0 and 100"),
            ("1", "1", "100.01", "tax_rate must be between 0 and 100"),
        ],
    )
    def test_rejects_out_of_range_input(
        self, unit_price: str, quantity: str, tax_rate: str, message: str
    ) -> None:
        with pytest.raises(ValueError, match=message):
            compute_line(Decimal(unit_price), Decimal(quantity), Decimal(tax_rate))

    @pytest.mark.parametrize("position", [0, 1, 2])
    def test_refuses_a_float_in_any_position(self, position: int) -> None:
        args: list[object] = [Decimal(1), Decimal(1), Decimal(0)]
        args[position] = 1.0

        with pytest.raises(TypeError, match="must be a Decimal"):
            compute_line(*args)  # type: ignore[arg-type]


class TestComputeInvoiceTotals:
    """Invoice totals (business rule 7)."""

    def test_sums_lines(self) -> None:
        lines = [
            compute_line(Decimal("500.00"), Decimal(1), ZERO),
            compute_line(Decimal("1200.00"), Decimal(1), Decimal(18)),
        ]

        totals = compute_invoice_totals(lines)

        assert totals.subtotal == Decimal("1700.00")
        assert totals.tax_amount == Decimal("216.00")
        assert totals.discount_amount == Decimal("0.00")
        assert totals.total == Decimal("1916.00")

    def test_no_lines_is_zero(self) -> None:
        totals = compute_invoice_totals([])

        assert totals.subtotal == totals.tax_amount == totals.total == ZERO

    def test_discount_is_subtracted(self) -> None:
        lines = [compute_line(Decimal("1000.00"), Decimal(1), Decimal(10))]

        totals = compute_invoice_totals(lines, discount_amount=Decimal("250.00"))

        # 1000 + 100 tax - 250 discount.
        assert totals.total == Decimal("850.00")

    def test_discount_may_equal_the_subtotal(self) -> None:
        lines = [compute_line(Decimal("100.00"), Decimal(1), ZERO)]

        assert compute_invoice_totals(lines, discount_amount=Decimal("100.00")).total == ZERO

    def test_rejects_a_discount_above_the_subtotal(self) -> None:
        lines = [compute_line(Decimal("100.00"), Decimal(1), ZERO)]

        with pytest.raises(ValueError, match="must not exceed the subtotal"):
            compute_invoice_totals(lines, discount_amount=Decimal("100.01"))

    def test_rejects_a_negative_discount(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            compute_invoice_totals([], discount_amount=Decimal("-1"))

    def test_refuses_a_float_discount(self) -> None:
        with pytest.raises(TypeError, match="must be a Decimal"):
            compute_invoice_totals([], discount_amount=1.0)  # type: ignore[arg-type]


class TestProperties:
    """Rules that must hold for every admissible input (spec §16, AC-7)."""

    @PROPERTY
    @given(line_inputs)
    def test_line_amounts_are_whole_paise(self, inputs: tuple[Decimal, Decimal, Decimal]) -> None:
        line = compute_line(*inputs)

        for amount in (line.net, line.tax, line.total):
            assert amount == amount.quantize(MONEY_QUANTUM)
            assert _places(amount) == 2

    @PROPERTY
    @given(line_inputs)
    def test_line_total_is_net_plus_tax(self, inputs: tuple[Decimal, Decimal, Decimal]) -> None:
        line = compute_line(*inputs)

        assert line.total == line.net + line.tax
        assert line.net >= 0
        assert line.tax >= 0

    @PROPERTY
    @given(line_inputs)
    def test_rounding_never_moves_a_line_by_more_than_half_a_paisa(
        self, inputs: tuple[Decimal, Decimal, Decimal]
    ) -> None:
        unit_price, quantity, tax_rate = inputs
        line = compute_line(unit_price, quantity, tax_rate)
        half_paisa = Decimal("0.005")

        assert abs(line.net - unit_price * quantity) <= half_paisa
        assert abs(line.tax - line.net * tax_rate / 100) <= half_paisa

    @PROPERTY
    @given(prices, quantities)
    def test_untaxed_line_has_no_tax(self, unit_price: Decimal, quantity: Decimal) -> None:
        line = compute_line(unit_price, quantity, ZERO)

        assert line.tax == ZERO
        assert line.total == line.net

    @PROPERTY
    @given(st.lists(line_inputs, max_size=30))
    def test_totals_equal_the_sum_of_the_lines(
        self, inputs: list[tuple[Decimal, Decimal, Decimal]]
    ) -> None:
        # AC-5: what is printed line by line adds up to what is printed as the
        # total, exactly — not to within a paisa.
        lines = [compute_line(*line) for line in inputs]

        totals = compute_invoice_totals(lines)

        assert totals.subtotal == sum((line.net for line in lines), ZERO)
        assert totals.tax_amount == sum((line.tax for line in lines), ZERO)
        assert totals.total == sum((line.total for line in lines), ZERO)

    @PROPERTY
    @given(st.lists(line_inputs, min_size=2, max_size=12), st.randoms(use_true_random=False))
    def test_line_order_does_not_change_the_total(
        self, inputs: list[tuple[Decimal, Decimal, Decimal]], rng: object
    ) -> None:
        lines = [compute_line(*line) for line in inputs]
        shuffled = list(lines)
        rng.shuffle(shuffled)  # type: ignore[attr-defined]

        assert compute_invoice_totals(shuffled) == compute_invoice_totals(lines)

    @PROPERTY
    @given(st.lists(line_inputs, min_size=1, max_size=12), st.data())
    def test_total_is_never_negative_for_an_allowed_discount(
        self, inputs: list[tuple[Decimal, Decimal, Decimal]], data: st.DataObject
    ) -> None:
        lines = [compute_line(*line) for line in inputs]
        subtotal = sum((line.net for line in lines), ZERO)
        discount = data.draw(st.decimals(min_value=Decimal(0), max_value=subtotal, places=2))

        totals = compute_invoice_totals(lines, discount_amount=discount)

        assert totals.total >= 0
        assert totals.total == totals.subtotal + totals.tax_amount - totals.discount_amount
