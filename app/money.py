"""Money arithmetic. All amounts are integer minor units (cents); percentages are integer
basis points (1% = 100 bp). No floats are involved anywhere."""


def percent_of(amount_cents: int, bp: int) -> int:
    """amount * bp / 10_000, rounded half-up to the nearest cent, using integer arithmetic."""
    if amount_cents < 0:
        raise ValueError("amount must be non-negative")
    if not 0 <= bp <= 10_000:
        raise ValueError("basis points must be within [0, 10000]")
    return (amount_cents * bp + 5_000) // 10_000


def apply_percent_discount(subtotal_cents: int, bp: int) -> tuple[int, int]:
    """Return (discount, total). The discount is capped at the subtotal so the total is never negative."""
    discount = min(percent_of(subtotal_cents, bp), subtotal_cents)
    return discount, subtotal_cents - discount


def format_bp(bp: int) -> str:
    """1000 -> "10.00", 1250 -> "12.50"."""
    return f"{bp // 100}.{bp % 100:02d}"
