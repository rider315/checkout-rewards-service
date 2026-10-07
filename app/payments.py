"""Payment seam. There is no real provider; `FakePaymentGateway` approves everything.

The seam exists so checkout has an explicit point where an external step can fail
*after* inventory and the coupon have been claimed, and tests can prove that such a
failure rolls both back. See DECISIONS.md for how this changes with a real provider.
"""
import uuid
from typing import Protocol


class PaymentGateway(Protocol):
    def charge(self, *, order_id: str, amount_cents: int, currency: str, idempotency_key: str) -> str:
        """Charge the customer; return a payment reference or raise errors.PaymentDeclined."""


class FakePaymentGateway:
    def charge(self, *, order_id: str, amount_cents: int, currency: str, idempotency_key: str) -> str:
        return f"fakepay_{uuid.uuid4().hex[:16]}"
