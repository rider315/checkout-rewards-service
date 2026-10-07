import sqlite3
from typing import Optional

from .db import read_transaction, utc_now, write_transaction
from .errors import InvalidRequest, NotFound


def _view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "unit_price_cents": row["unit_price_cents"],
        "stock": row["stock"],
    }


def list_products(conn: sqlite3.Connection) -> list[dict]:
    with read_transaction(conn):
        rows = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return [_view(r) for r in rows]


def get_product(conn: sqlite3.Connection, product_id: str) -> dict:
    with read_transaction(conn):
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if row is None:
        raise NotFound("PRODUCT_NOT_FOUND", f"Product {product_id!r} does not exist.")
    return _view(row)


def update_product(
    conn: sqlite3.Connection,
    product_id: str,
    unit_price_cents: Optional[int],
    stock: Optional[int],
) -> dict:
    """Admin: change price and/or set absolute stock. Existing orders are unaffected because
    they snapshot prices; open carts see the new values on their next read."""
    if unit_price_cents is None and stock is None:
        raise InvalidRequest("VALIDATION_ERROR", "Provide unit_price_cents and/or stock.")
    if (unit_price_cents is not None and unit_price_cents < 0) or (stock is not None and stock < 0):
        raise InvalidRequest("VALIDATION_ERROR", "unit_price_cents and stock must be >= 0.")
    with write_transaction(conn):
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if row is None:
            raise NotFound("PRODUCT_NOT_FOUND", f"Product {product_id!r} does not exist.")
        conn.execute(
            "UPDATE products SET unit_price_cents = ?, stock = ?, updated_at = ? WHERE id = ?",
            (
                row["unit_price_cents"] if unit_price_cents is None else unit_price_cents,
                row["stock"] if stock is None else stock,
                utc_now(),
                product_id,
            ),
        )
        row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    return _view(row)
