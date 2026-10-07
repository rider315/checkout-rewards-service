"""Carts hold (product, quantity) references, not price locks. Viewing a cart always prices it
at current catalog prices and flags lines whose price or availability changed since the
customer last set them."""
import sqlite3
import uuid

from .config import Settings
from .db import read_transaction, utc_now, write_transaction
from .errors import Conflict, InvalidRequest, NotFound, Unprocessable


def _load_open_cart(conn: sqlite3.Connection, cart_id: str) -> sqlite3.Row:
    cart = conn.execute("SELECT * FROM carts WHERE id = ?", (cart_id,)).fetchone()
    if cart is None:
        raise NotFound("CART_NOT_FOUND", f"Cart {cart_id!r} does not exist.")
    if cart["status"] != "open":
        raise Conflict(
            "CART_NOT_OPEN",
            "The cart has already been checked out and can no longer be changed.",
            {"cart_id": cart_id, "order_id": _order_id_for_cart(conn, cart_id)},
        )
    return cart


def _order_id_for_cart(conn: sqlite3.Connection, cart_id: str):
    row = conn.execute("SELECT id FROM orders WHERE cart_id = ?", (cart_id,)).fetchone()
    return row["id"] if row else None


def _build_view(conn: sqlite3.Connection, cart_id: str, settings: Settings) -> dict:
    cart = conn.execute("SELECT * FROM carts WHERE id = ?", (cart_id,)).fetchone()
    if cart is None:
        raise NotFound("CART_NOT_FOUND", f"Cart {cart_id!r} does not exist.")
    rows = conn.execute(
        """
        SELECT ci.product_id, ci.quantity, ci.unit_price_cents_at_set,
               p.name, p.unit_price_cents, p.stock
        FROM cart_items ci JOIN products p ON p.id = ci.product_id
        WHERE ci.cart_id = ?
        ORDER BY ci.product_id
        """,
        (cart_id,),
    ).fetchall()

    items, issues = [], []
    for r in rows:
        price_changed = r["unit_price_cents"] != r["unit_price_cents_at_set"]
        in_stock = r["quantity"] <= r["stock"]
        items.append(
            {
                "product_id": r["product_id"],
                "name": r["name"],
                "quantity": r["quantity"],
                "unit_price_cents": r["unit_price_cents"],
                "unit_price_cents_when_added": r["unit_price_cents_at_set"],
                "price_changed": price_changed,
                "line_total_cents": r["unit_price_cents"] * r["quantity"],
                "available_stock": r["stock"],
                "sufficient_stock": in_stock,
            }
        )
        if price_changed:
            issues.append({"code": "PRICE_CHANGED", "product_id": r["product_id"]})
        if not in_stock:
            issues.append({"code": "INSUFFICIENT_INVENTORY", "product_id": r["product_id"]})

    is_open = cart["status"] == "open"
    return {
        "id": cart["id"],
        "status": cart["status"],
        "order_id": None if is_open else _order_id_for_cart(conn, cart_id),
        "currency": settings.currency,
        "items": items,
        "item_count": sum(i["quantity"] for i in items),
        "subtotal_cents": sum(i["line_total_cents"] for i in items),
        # Advisory only: checkout re-validates everything atomically.
        "issues": issues,
        "checkout_ready": is_open and bool(items) and not any(not i["sufficient_stock"] for i in items),
        "created_at": cart["created_at"],
        "updated_at": cart["updated_at"],
    }


def create_cart(conn: sqlite3.Connection, settings: Settings) -> dict:
    cart_id = f"cart_{uuid.uuid4().hex}"
    now = utc_now()
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO carts (id, status, created_at, updated_at) VALUES (?, 'open', ?, ?)",
            (cart_id, now, now),
        )
        return _build_view(conn, cart_id, settings)


def get_cart(conn: sqlite3.Connection, cart_id: str, settings: Settings) -> dict:
    with read_transaction(conn):
        return _build_view(conn, cart_id, settings)


def set_item_quantity(
    conn: sqlite3.Connection, cart_id: str, product_id: str, quantity: int, settings: Settings
) -> tuple[dict, bool]:
    """Add a product or replace its quantity (absolute, so retries are harmless).
    Returns (cart view, created) where created is True if the line did not exist before."""
    if not 1 <= quantity <= settings.max_line_quantity:
        raise InvalidRequest(
            "INVALID_QUANTITY",
            f"Quantity must be an integer between 1 and {settings.max_line_quantity}; "
            "use DELETE to remove an item.",
            {"quantity": quantity, "min": 1, "max": settings.max_line_quantity},
        )
    with write_transaction(conn):
        _load_open_cart(conn, cart_id)
        product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            raise Unprocessable("UNKNOWN_PRODUCT", f"Product {product_id!r} does not exist.", {"product_id": product_id})
        # Fail fast on quantities that cannot currently be fulfilled. This is not a
        # reservation: stock is only claimed at checkout.
        if quantity > product["stock"]:
            raise Conflict(
                "INSUFFICIENT_INVENTORY",
                f"Only {product['stock']} unit(s) of {product_id!r} are available.",
                {"items": [{"product_id": product_id, "requested": quantity, "available": product["stock"]}]},
            )
        now = utc_now()
        created = (
            conn.execute(
                "SELECT 1 FROM cart_items WHERE cart_id = ? AND product_id = ?", (cart_id, product_id)
            ).fetchone()
            is None
        )
        conn.execute(
            """
            INSERT INTO cart_items (cart_id, product_id, quantity, unit_price_cents_at_set, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (cart_id, product_id) DO UPDATE SET
                quantity = excluded.quantity,
                unit_price_cents_at_set = excluded.unit_price_cents_at_set,
                updated_at = excluded.updated_at
            """,
            (cart_id, product_id, quantity, product["unit_price_cents"], now),
        )
        conn.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (now, cart_id))
        return _build_view(conn, cart_id, settings), created


def remove_item(conn: sqlite3.Connection, cart_id: str, product_id: str, settings: Settings) -> dict:
    with write_transaction(conn):
        _load_open_cart(conn, cart_id)
        deleted = conn.execute(
            "DELETE FROM cart_items WHERE cart_id = ? AND product_id = ?", (cart_id, product_id)
        ).rowcount
        if not deleted:
            raise NotFound("ITEM_NOT_IN_CART", f"Product {product_id!r} is not in the cart.", {"product_id": product_id})
        conn.execute("UPDATE carts SET updated_at = ? WHERE id = ?", (utc_now(), cart_id))
        return _build_view(conn, cart_id, settings)
