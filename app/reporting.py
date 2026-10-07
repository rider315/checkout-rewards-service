"""Admin summary. Read-only, and computed from a single snapshot so every figure in one
response describes the same set of orders and coupons."""
import sqlite3

from .config import Settings
from .coupons import milestone_status
from .db import read_transaction


def build_report(conn: sqlite3.Connection, settings: Settings) -> dict:
    with read_transaction(conn):
        products = conn.execute(
            """
            SELECT p.id, p.name,
                   COALESCE(SUM(ol.quantity), 0)         AS quantity_sold,
                   COALESCE(SUM(ol.line_total_cents), 0) AS gross_revenue_cents
            FROM products p LEFT JOIN order_lines ol ON ol.product_id = p.id
            GROUP BY p.id, p.name
            ORDER BY p.id
            """
        ).fetchall()
        totals = conn.execute(
            """
            SELECT COUNT(*)                         AS placed,
                   COALESCE(SUM(subtotal_cents), 0) AS gross,
                   COALESCE(SUM(discount_cents), 0) AS discounts,
                   COALESCE(SUM(total_cents), 0)    AS net
            FROM orders
            """
        ).fetchone()
        coupons = conn.execute(
            """
            SELECT COUNT(*)                                             AS generated,
                   COALESCE(SUM(status = 'available'), 0)               AS available,
                   COALESCE(SUM(status = 'redeemed'), 0)                AS redeemed
            FROM coupons
            """
        ).fetchone()
        milestones = milestone_status(conn, settings)

    return {
        "currency": settings.currency,
        "orders_placed": totals["placed"],
        "products": [
            {
                "product_id": p["id"],
                "name": p["name"],
                "quantity_sold": p["quantity_sold"],
                "gross_revenue_cents": p["gross_revenue_cents"],
            }
            for p in products
        ],
        "revenue": {
            "gross_cents": totals["gross"],
            "discounts_cents": totals["discounts"],
            "net_cents": totals["net"],
        },
        "coupons": {
            "generated": coupons["generated"],
            "available": coupons["available"],
            "redeemed": coupons["redeemed"],
        },
        "milestones": {
            "every_n_orders": milestones["every_n_orders"],
            "percent_off": milestones["percent_off"],
            "eligible": milestones["eligible_milestones"],
            "rewarded": milestones["rewarded_milestones"],
            "pending": len(milestones["pending_milestones"]),
            "next_milestone_at": milestones["next_milestone_at"],
        },
    }
