import sqlite3

from .db import read_transaction
from .errors import NotFound
from .money import format_bp


def load_order(conn: sqlite3.Connection, order_id: str) -> dict:
    """Build an order view purely from the order's own snapshot rows, never from live
    product data, so it reads the same after catalog prices or names change."""
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if order is None:
        raise NotFound("ORDER_NOT_FOUND", f"Order {order_id!r} does not exist.")
    lines = conn.execute(
        "SELECT * FROM order_lines WHERE order_id = ? ORDER BY line_no", (order_id,)
    ).fetchall()
    return {
        "id": order["id"],
        "order_number": order["order_number"],
        "status": order["status"],
        "cart_id": order["cart_id"],
        "currency": order["currency"],
        "lines": [
            {
                "product_id": l["product_id"],
                "product_name": l["product_name"],
                "unit_price_cents": l["unit_price_cents"],
                "quantity": l["quantity"],
                "line_total_cents": l["line_total_cents"],
            }
            for l in lines
        ],
        "subtotal_cents": order["subtotal_cents"],
        "coupon": None
        if order["coupon_code"] is None
        else {"code": order["coupon_code"], "percent_off": format_bp(order["discount_bp"])},
        "discount_cents": order["discount_cents"],
        "total_cents": order["total_cents"],
        "payment_reference": order["payment_reference"],
        "created_at": order["created_at"],
    }


def get_order(conn: sqlite3.Connection, order_id: str) -> dict:
    with read_transaction(conn):
        return load_order(conn, order_id)


def list_orders(conn: sqlite3.Connection) -> list[dict]:
    with read_transaction(conn):
        ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY order_number")]
        return [load_order(conn, i) for i in ids]
