"""SQLite access: schema, seed data and transaction helpers.

Concurrency model: every state-changing use case runs inside `write_transaction`,
which opens the transaction with BEGIN IMMEDIATE. That takes SQLite's single
database-wide write lock *before* anything is read, so a use case's reads and
writes form one serialized unit even when requests overlap, and even across
several OS processes sharing the same database file. Reads use
`read_transaction`, which in WAL mode gives a consistent snapshot without
blocking writers.
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    stock            INTEGER NOT NULL CHECK (stock >= 0),
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carts (
    id         TEXT PRIMARY KEY,
    status     TEXT NOT NULL CHECK (status IN ('open', 'checked_out')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cart_items (
    cart_id                 TEXT NOT NULL REFERENCES carts(id),
    product_id              TEXT NOT NULL REFERENCES products(id),
    quantity                INTEGER NOT NULL CHECK (quantity > 0),
    -- Price the customer saw when they last set this line; informational only,
    -- checkout always charges the current price (see DECISIONS.md).
    unit_price_cents_at_set INTEGER NOT NULL,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (cart_id, product_id)
);

CREATE TABLE IF NOT EXISTS coupons (
    code                   TEXT PRIMARY KEY,
    -- The placed-order count that earned this coupon (n, 2n, 3n, ...).
    -- UNIQUE: at most one coupon per milestone, enforced by the database.
    milestone_order_number INTEGER NOT NULL UNIQUE,
    discount_bp            INTEGER NOT NULL CHECK (discount_bp > 0 AND discount_bp <= 10000),
    status                 TEXT NOT NULL CHECK (status IN ('available', 'redeemed')),
    created_at             TEXT NOT NULL,
    redeemed_order_id      TEXT UNIQUE,
    redeemed_at            TEXT,
    CHECK ((status = 'redeemed') = (redeemed_order_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    order_number        INTEGER NOT NULL UNIQUE,
    cart_id             TEXT NOT NULL UNIQUE REFERENCES carts(id),
    idempotency_key     TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status = 'placed'),
    currency            TEXT NOT NULL,
    subtotal_cents      INTEGER NOT NULL CHECK (subtotal_cents >= 0),
    discount_cents      INTEGER NOT NULL CHECK (discount_cents >= 0 AND discount_cents <= subtotal_cents),
    total_cents         INTEGER NOT NULL CHECK (total_cents = subtotal_cents - discount_cents),
    -- UNIQUE: a coupon can appear on at most one order, enforced by the database.
    coupon_code         TEXT UNIQUE REFERENCES coupons(code),
    discount_bp         INTEGER,
    payment_reference   TEXT,
    created_at          TEXT NOT NULL,
    CHECK ((coupon_code IS NULL) = (discount_bp IS NULL))
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id         TEXT NOT NULL REFERENCES orders(id),
    line_no          INTEGER NOT NULL,
    product_id       TEXT NOT NULL REFERENCES products(id),
    product_name     TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    quantity         INTEGER NOT NULL CHECK (quantity > 0),
    line_total_cents INTEGER NOT NULL CHECK (line_total_cents = unit_price_cents * quantity),
    PRIMARY KEY (order_id, line_no)
);
"""

# (id, name, unit price in cents, initial stock)
SEED_PRODUCTS = [
    ("keyboard", "Mechanical Keyboard", 8999, 50),
    ("mouse", "Wireless Mouse", 2999, 100),
    ("monitor", "27-inch Monitor", 24999, 20),
    ("usb-cable", "USB-C Cable", 999, 500),
    ("sticker-pack", "Sticker Pack", 333, 1000),
    # Deliberately scarce: used to demonstrate that concurrent checkouts cannot oversell.
    ("limited-headphones", "Limited Edition Headphones", 19999, 3),
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None: we issue BEGIN/COMMIT ourselves instead of relying on
    # the sqlite3 module's implicit transactions.
    # check_same_thread=False: FastAPI may run a request's dependency and its
    # handler on different worker threads; a connection is still only ever used
    # by one request at a time.
    conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block under the database write lock; commit on success, roll back on any error."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run several reads against one consistent snapshot."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("ROLLBACK")


def init_db(path: str) -> None:
    """Create the schema and insert any missing seed products. Safe to run repeatedly."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)  # every statement is IF NOT EXISTS, so this is idempotent
        with write_transaction(conn):
            now = utc_now()
            # INSERT OR IGNORE: restarting never overwrites price/stock changed at runtime.
            conn.executemany(
                "INSERT OR IGNORE INTO products (id, name, unit_price_cents, stock, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(*p, now) for p in SEED_PRODUCTS],
            )
    finally:
        conn.close()
