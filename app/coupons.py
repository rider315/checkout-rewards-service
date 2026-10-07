"""Milestone coupons: every n-th placed order earns one coupon, generated on admin request.

Milestones are placed-order counts n, 2n, 3n, ... A milestone is *eligible* once that many
orders exist and *rewarded* once a coupon row records it. Generation always rewards the
oldest eligible-but-unrewarded milestone, one per request.
"""
import secrets
import sqlite3

from .config import Settings
from .db import read_transaction, utc_now, write_transaction
from .errors import Conflict
from .money import format_bp

# No 0/O/1/I so codes survive being read aloud or retyped.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_code(settings: Settings) -> str:
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(10))
    return f"SAVE{settings.coupon_discount_bp // 100}-{suffix}"


def _view(row: sqlite3.Row) -> dict:
    return {
        "code": row["code"],
        "milestone_order_number": row["milestone_order_number"],
        "percent_off": format_bp(row["discount_bp"]),
        "status": row["status"],
        "created_at": row["created_at"],
        "redeemed_order_id": row["redeemed_order_id"],
        "redeemed_at": row["redeemed_at"],
    }


def milestone_status(conn: sqlite3.Connection, settings: Settings) -> dict:
    n = settings.coupon_every_n_orders
    placed = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    rewarded = {r[0] for r in conn.execute("SELECT milestone_order_number FROM coupons")}
    eligible = [k * n for k in range(1, placed // n + 1)]
    pending = [m for m in eligible if m not in rewarded]
    return {
        "every_n_orders": n,
        "percent_off": format_bp(settings.coupon_discount_bp),
        "placed_orders": placed,
        "eligible_milestones": len(eligible),
        "rewarded_milestones": len(rewarded),
        "pending_milestones": pending,
        "next_milestone_at": (placed // n + 1) * n,
    }


def generate_coupon(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Admin: create the coupon for the oldest unrewarded milestone, or fail with
    NO_ELIGIBLE_MILESTONE. Runs under the write lock, so concurrent requests cannot both
    reward the same milestone (the UNIQUE constraint on the milestone backs this up)."""
    with write_transaction(conn):
        status = milestone_status(conn, settings)
        if not status["pending_milestones"]:
            raise Conflict(
                "NO_ELIGIBLE_MILESTONE",
                "No order milestone is waiting for a coupon.",
                {k: status[k] for k in ("placed_orders", "every_n_orders", "rewarded_milestones", "next_milestone_at")},
            )
        milestone = status["pending_milestones"][0]
        code = _new_code(settings)
        conn.execute(
            "INSERT INTO coupons (code, milestone_order_number, discount_bp, status, created_at) "
            "VALUES (?, ?, ?, 'available', ?)",
            (code, milestone, settings.coupon_discount_bp, utc_now()),
        )
        coupon = _view(conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone())
        return {"coupon": coupon, "remaining_pending_milestones": len(status["pending_milestones"]) - 1}


def list_coupons(conn: sqlite3.Connection, settings: Settings) -> dict:
    with read_transaction(conn):
        rows = conn.execute("SELECT * FROM coupons ORDER BY milestone_order_number").fetchall()
        return {"coupons": [_view(r) for r in rows], "milestones": milestone_status(conn, settings)}
