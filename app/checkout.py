"""Checkout: turns an open cart into a placed order, exactly once.

Everything below happens inside one BEGIN IMMEDIATE transaction, so the sequence
check -> claim stock -> claim coupon -> charge -> record order is all-or-nothing and
cannot interleave with another checkout, cart edit or coupon generation. If any step
raises, the rollback releases the stock and the coupon as if nothing happened.
"""
import hashlib
import json
import sqlite3
import uuid
from typing import Optional

from .config import Settings
from .db import utc_now, write_transaction
from .errors import Conflict, InvalidRequest, NotFound, Unprocessable
from .money import apply_percent_discount
from .orders import load_order
from .payments import PaymentGateway

MAX_IDEMPOTENCY_KEY_LENGTH = 255


def normalize_coupon_code(code: Optional[str]) -> Optional[str]:
    if code is None:
        return None
    code = code.strip().upper()
    return code or None


def _fingerprint(cart_id: str, coupon_code: Optional[str], expected_subtotal_cents: Optional[int]) -> str:
    payload = json.dumps(
        {"cart_id": cart_id, "coupon_code": coupon_code, "expected_subtotal_cents": expected_subtotal_cents},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def checkout(
    conn: sqlite3.Connection,
    settings: Settings,
    gateway: PaymentGateway,
    *,
    cart_id: str,
    idempotency_key: Optional[str],
    coupon_code: Optional[str] = None,
    expected_subtotal_cents: Optional[int] = None,
) -> tuple[dict, bool]:
    """Return (order view, replayed). `replayed` is True when this request is a retry of an
    already-successful checkout and the stored order is returned instead of a new one."""
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidRequest(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Send an Idempotency-Key header (e.g. a UUID) and reuse it when retrying this checkout.",
        )
    if len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidRequest("VALIDATION_ERROR", f"Idempotency-Key must be at most {MAX_IDEMPOTENCY_KEY_LENGTH} characters.")

    coupon_code = normalize_coupon_code(coupon_code)
    fingerprint = _fingerprint(cart_id, coupon_code, expected_subtotal_cents)

    with write_transaction(conn):
        # 1. Replay: a previous attempt with this key already succeeded.
        prior = conn.execute(
            "SELECT id, request_fingerprint FROM orders WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if prior is not None:
            if prior["request_fingerprint"] != fingerprint:
                raise Unprocessable(
                    "IDEMPOTENCY_KEY_REUSED",
                    "This Idempotency-Key was already used for a different checkout request.",
                    {"order_id": prior["id"]},
                )
            return load_order(conn, prior["id"]), True

        # 2. The cart must exist and still be open (single checkout per cart).
        cart = conn.execute("SELECT status FROM carts WHERE id = ?", (cart_id,)).fetchone()
        if cart is None:
            raise NotFound("CART_NOT_FOUND", f"Cart {cart_id!r} does not exist.")
        if cart["status"] != "open":
            order = conn.execute("SELECT id FROM orders WHERE cart_id = ?", (cart_id,)).fetchone()
            raise Conflict(
                "CART_ALREADY_CHECKED_OUT",
                "This cart has already been checked out.",
                {"order_id": order["id"] if order else None},
            )

        # 3. Price the cart at current prices and verify stock for every line.
        lines = conn.execute(
            """
            SELECT ci.product_id, ci.quantity, ci.unit_price_cents_at_set,
                   p.name, p.unit_price_cents, p.stock
            FROM cart_items ci JOIN products p ON p.id = ci.product_id
            WHERE ci.cart_id = ?
            ORDER BY ci.product_id
            """,
            (cart_id,),
        ).fetchall()
        if not lines:
            raise Unprocessable("CART_EMPTY", "Cannot check out an empty cart.")

        shortages = [
            {"product_id": l["product_id"], "requested": l["quantity"], "available": l["stock"]}
            for l in lines
            if l["quantity"] > l["stock"]
        ]
        if shortages:
            raise Conflict(
                "INSUFFICIENT_INVENTORY",
                "Not enough stock for one or more items; nothing was charged.",
                {"items": shortages},
            )

        subtotal = sum(l["unit_price_cents"] * l["quantity"] for l in lines)
        if expected_subtotal_cents is not None and expected_subtotal_cents != subtotal:
            raise Conflict(
                "PRICE_CHANGED",
                "The cart subtotal no longer matches the expected amount; review the cart and retry.",
                {
                    "expected_subtotal_cents": expected_subtotal_cents,
                    "current_subtotal_cents": subtotal,
                    "items": [
                        {
                            "product_id": l["product_id"],
                            "unit_price_cents_when_added": l["unit_price_cents_at_set"],
                            "unit_price_cents": l["unit_price_cents"],
                        }
                        for l in lines
                    ],
                },
            )

        # 4. Validate the coupon (it is claimed below, in the same transaction).
        discount_bp = None
        if coupon_code is not None:
            coupon = conn.execute("SELECT * FROM coupons WHERE code = ?", (coupon_code,)).fetchone()
            if coupon is None:
                raise Unprocessable("COUPON_NOT_FOUND", f"Coupon {coupon_code!r} does not exist.")
            if coupon["status"] != "available":
                raise Conflict("COUPON_ALREADY_REDEEMED", f"Coupon {coupon_code!r} has already been redeemed.")
            discount_bp = coupon["discount_bp"]

        discount, total = apply_percent_discount(subtotal, discount_bp or 0)
        order_id = f"ord_{uuid.uuid4().hex}"
        now = utc_now()

        # 5. Claim stock. The guarded UPDATE re-checks availability at write time; under the
        #    write lock it cannot fail, but it keeps the invariant local to the statement
        #    (and is what would protect us on a database with row-level locking).
        for l in lines:
            claimed = conn.execute(
                "UPDATE products SET stock = stock - ?, updated_at = ? WHERE id = ? AND stock >= ?",
                (l["quantity"], now, l["product_id"], l["quantity"]),
            ).rowcount
            if claimed != 1:
                raise Conflict(
                    "INSUFFICIENT_INVENTORY",
                    "Not enough stock for one or more items; nothing was charged.",
                    {"items": [{"product_id": l["product_id"], "requested": l["quantity"]}]},
                )

        # 6. Claim the coupon (same guarded-update pattern).
        if coupon_code is not None:
            claimed = conn.execute(
                "UPDATE coupons SET status = 'redeemed', redeemed_order_id = ?, redeemed_at = ? "
                "WHERE code = ? AND status = 'available'",
                (order_id, now, coupon_code),
            ).rowcount
            if claimed != 1:
                raise Conflict("COUPON_ALREADY_REDEEMED", f"Coupon {coupon_code!r} has already been redeemed.")

        # 7. Close the cart.
        closed = conn.execute(
            "UPDATE carts SET status = 'checked_out', updated_at = ? WHERE id = ? AND status = 'open'",
            (now, cart_id),
        ).rowcount
        if closed != 1:
            raise Conflict("CART_ALREADY_CHECKED_OUT", "This cart has already been checked out.")

        # 8. Take payment. A decline raises and rolls back steps 5-7.
        payment_reference = None
        if total > 0:
            payment_reference = gateway.charge(
                order_id=order_id, amount_cents=total, currency=settings.currency, idempotency_key=idempotency_key
            )

        # 9. Record the order with a full price snapshot.
        order_number = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] + 1
        conn.execute(
            """
            INSERT INTO orders (id, order_number, cart_id, idempotency_key, request_fingerprint, status,
                                currency, subtotal_cents, discount_cents, total_cents, coupon_code,
                                discount_bp, payment_reference, created_at)
            VALUES (?, ?, ?, ?, ?, 'placed', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_id, order_number, cart_id, idempotency_key, fingerprint, settings.currency,
                subtotal, discount, total, coupon_code, discount_bp, payment_reference, now,
            ),
        )
        conn.executemany(
            """
            INSERT INTO order_lines (order_id, line_no, product_id, product_name, unit_price_cents,
                                     quantity, line_total_cents)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (order_id, n, l["product_id"], l["name"], l["unit_price_cents"], l["quantity"],
                 l["unit_price_cents"] * l["quantity"])
                for n, l in enumerate(lines, start=1)
            ],
        )
        return load_order(conn, order_id), False
