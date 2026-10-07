import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


def percent_to_basis_points(value: str) -> int:
    """Parse a percentage such as "10" or "12.5" into basis points (10% -> 1000).

    Rejects anything that cannot be represented exactly in basis points, so a
    configured discount never silently changes precision.
    """
    try:
        bp = Decimal(value) * 100
    except InvalidOperation as exc:
        raise ValueError(f"discount percent {value!r} is not a number") from exc
    if bp != bp.to_integral_value():
        raise ValueError(f"discount percent {value!r} has more than 2 decimal places")
    bp_int = int(bp)
    if not 0 < bp_int <= 10_000:
        raise ValueError(f"discount percent {value!r} must be > 0 and <= 100")
    return bp_int


@dataclass(frozen=True)
class Settings:
    database_path: str
    # A coupon becomes available every `coupon_every_n_orders` placed orders ...
    coupon_every_n_orders: int
    # ... worth this many basis points off the order subtotal (1000 = 10%).
    coupon_discount_bp: int
    currency: str = "USD"
    # Upper bound per cart line; guards against absurd quantities and integer overflow.
    max_line_quantity: int = 100

    def __post_init__(self) -> None:
        if self.coupon_every_n_orders < 1:
            raise ValueError("coupon_every_n_orders must be >= 1")
        if not 0 < self.coupon_discount_bp <= 10_000:
            raise ValueError("coupon_discount_bp must be in (0, 10000]")

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            database_path=os.environ.get("DATABASE_PATH", os.path.join("data", "store.db")),
            coupon_every_n_orders=int(os.environ.get("COUPON_EVERY_N_ORDERS", "5")),
            coupon_discount_bp=percent_to_basis_points(os.environ.get("COUPON_DISCOUNT_PERCENT", "10")),
        )
