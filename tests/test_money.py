import pytest

from app.config import percent_to_basis_points
from app.money import apply_percent_discount, percent_of


@pytest.mark.parametrize(
    "amount, bp, expected",
    [
        (333, 1000, 33),     # 33.3  -> 33
        (1665, 1000, 167),   # 166.5 -> 167 (half rounds up)
        (1664, 1000, 166),   # 166.4 -> 166
        (5, 1000, 1),        # 0.5   -> 1
        (4, 1000, 0),        # 0.4   -> 0
        (999, 1250, 125),    # 12.5% of 9.99 = 124.875 -> 125
        (0, 1000, 0),
        (10**15, 3333, 333_300_000_000_000),  # large amounts stay exact (no float)
    ],
)
def test_percent_of_rounds_half_up_to_the_cent(amount, bp, expected):
    assert percent_of(amount, bp) == expected


def test_discount_never_makes_total_negative():
    assert apply_percent_discount(1, 10_000) == (1, 0)
    assert apply_percent_discount(0, 10_000) == (0, 0)
    for subtotal in range(0, 2000, 7):
        discount, total = apply_percent_discount(subtotal, 10_000)
        assert total == 0 and discount == subtotal


@pytest.mark.parametrize("value, bp", [("10", 1000), ("12.5", 1250), ("0.01", 1), ("100", 10_000)])
def test_discount_percent_config_parses_exactly(value, bp):
    assert percent_to_basis_points(value) == bp


@pytest.mark.parametrize("value", ["0", "-5", "100.01", "12.345", "ten"])
def test_discount_percent_config_rejects_invalid_values(value):
    with pytest.raises(ValueError):
        percent_to_basis_points(value)
