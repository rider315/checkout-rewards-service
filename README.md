# Checkout and Rewards Service

Backend for a small store: carts, idempotent checkout that cannot oversell, and a
milestone coupon program (every *n*th placed order earns one coupon worth *x*% off).

- **Stack:** Python 3.11+, FastAPI, SQLite (standard library, WAL mode). No external services or credentials.
- **Design rationale:** [DECISIONS.md](DECISIONS.md), covering invariants, concurrency, idempotency, money and trade-offs.
- **Time spent:** _approximately N hours (fill in before submitting)_.

## Setup and run

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

On first start the database is created at `data/store.db` and the catalog is seeded.
Restarting keeps the data. To reset the store, stop the server and delete `data/`.

The invariants hold across processes because the database file holds the lock, so this also works:

```bash
uvicorn app.main:app --port 8000 --workers 4
```

Interactive OpenAPI docs are at `http://127.0.0.1:8000/docs`, and the raw schema is at `/openapi.json`.

### Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `COUPON_EVERY_N_ORDERS` | `5` | *n*: a coupon milestone occurs every n placed orders |
| `COUPON_DISCOUNT_PERCENT` | `10` | *x*: percent off the order subtotal, `0 < x <= 100`, up to 2 decimals |
| `DATABASE_PATH` | `data/store.db` | SQLite file location |

### Tests

```bash
python -m pytest -q
```

The tests start a real uvicorn server on a temporary database and send concurrent HTTP
requests released together by a barrier. They cover oversell races, duplicate retries,
double redemption of one coupon, racing coupon generation, and a mixed workload that must
reconcile with the report. The suite takes about 20 seconds.

### Executable walkthrough

With the server running (use a scratch database, because the script places orders):

```bash
python scripts/demo.py http://127.0.0.1:8000
```

## Seed data

| id | name | unit price | stock |
|---|---|---|---|
| `keyboard` | Mechanical Keyboard | 8999 ($89.99) | 50 |
| `mouse` | Wireless Mouse | 2999 | 100 |
| `monitor` | 27-inch Monitor | 24999 | 20 |
| `usb-cable` | USB-C Cable | 999 | 500 |
| `sticker-pack` | Sticker Pack | 333 | 1000 |
| `limited-headphones` | Limited Edition Headphones | 19999 | **3** (limited) |

## API

### Conventions

- JSON in and out. Request bodies reject unknown fields and type coercion, so `"2"` or `1.5` for an integer is an error.
- **Money is always integer minor units (cents)** in fields ending in `_cents`. The currency is `USD`. Percentages are decimal strings such as `"10.00"`.
- **Administrative operations are everything under `/admin`.** Authentication is out of scope, so in production these routes would sit behind an admin role.
- Every error uses one envelope. Clients should branch on `code`, not on `message`:

```json
{ "error": { "code": "INSUFFICIENT_INVENTORY", "message": "human readable", "details": { "items": [ { "product_id": "limited-headphones", "requested": 2, "available": 1 } ] } } }
```

### Endpoint summary

| Method | Path | Purpose | Success |
|---|---|---|---|
| GET | `/products` | List catalog | 200 |
| GET | `/products/{productId}` | One product | 200 |
| POST | `/carts` | Create cart | 201 |
| GET | `/carts/{cartId}` | View cart with current prices, totals, issues | 200 |
| PUT | `/carts/{cartId}/items/{productId}` | Add item or set its quantity | 201 (added) / 200 (changed) |
| DELETE | `/carts/{cartId}/items/{productId}` | Remove item | 200 |
| POST | `/carts/{cartId}/checkout` | Place order (idempotent, optional coupon) | 201 / 200 (replay) |
| GET | `/orders/{orderId}` | Retrieve order | 200 |
| PATCH | `/admin/products/{productId}` | **Admin:** change price and/or stock | 200 |
| POST | `/admin/coupons` | **Admin:** generate coupon for an eligible milestone | 201 |
| GET | `/admin/coupons` | **Admin:** list coupons and milestone status | 200 |
| GET | `/admin/orders` | **Admin:** list all orders | 200 |
| GET | `/admin/report` | **Admin:** sales and coupon summary | 200 |

---

#### `POST /carts`
No body. Returns **201** with a `Location: /carts/{id}` header and the cart view (shown below).

#### `GET /carts/{cartId}`
**200** returns the cart, priced at **current** catalog prices:

```json
{
  "id": "cart_9f…", "status": "open", "order_id": null, "currency": "USD",
  "items": [{
    "product_id": "mouse", "name": "Wireless Mouse", "quantity": 2,
    "unit_price_cents": 3499, "unit_price_cents_when_added": 2999, "price_changed": true,
    "line_total_cents": 6998, "available_stock": 100, "sufficient_stock": true
  }],
  "item_count": 2, "subtotal_cents": 6998,
  "issues": [{ "code": "PRICE_CHANGED", "product_id": "mouse" }],
  "checkout_ready": true, "created_at": "…", "updated_at": "…"
}
```
`issues` and `checkout_ready` are advisory. Checkout re-validates everything atomically.
After checkout, `status` is `checked_out` and `order_id` is set.
Errors: **404** `CART_NOT_FOUND`.

#### `PUT /carts/{cartId}/items/{productId}`
Body: `{"quantity": 2}`. This sets the **absolute** quantity. It adds the product if it is not
in the cart and replaces the quantity if it is. Because the quantity is absolute, a retry has no
additional effect. Returns **201** if the line was created, **200** if it was updated, with the cart view.

| Status | Code | When |
|---|---|---|
| 400 | `INVALID_QUANTITY` | quantity not in 1–100. Use DELETE to remove an item. |
| 400 | `VALIDATION_ERROR` | missing/non-integer quantity, unknown fields |
| 404 | `CART_NOT_FOUND` | |
| 409 | `CART_NOT_OPEN` | cart already checked out (`details.order_id`) |
| 409 | `INSUFFICIENT_INVENTORY` | quantity exceeds current stock (`details.items[].available`) |
| 422 | `UNKNOWN_PRODUCT` | product does not exist |

#### `DELETE /carts/{cartId}/items/{productId}`
Returns **200** with the cart view. Errors: **404** `CART_NOT_FOUND`, **404** `ITEM_NOT_IN_CART`, **409** `CART_NOT_OPEN`.

#### `POST /carts/{cartId}/checkout`
Headers: **`Idempotency-Key: <unique string, e.g. UUID>` (required)**. Generate one key per
checkout attempt and **reuse the same key when retrying** after a timeout or a lost response.

Body (optional, may be empty):
```json
{ "coupon_code": "SAVE10-ABCDEFGHJK", "expected_subtotal_cents": 6998 }
```
- `coupon_code` is case-insensitive and surrounding whitespace is ignored.
- `expected_subtotal_cents` is an optional price guard. If it is set and the cart now prices
  differently, checkout fails with `PRICE_CHANGED` instead of charging an amount the customer did not see.

**201** means the order was placed. **200** means this is a replay of an earlier successful
request with the same key and the same body. Both return the order (see below) with
`Location: /orders/{id}` and `Idempotent-Replayed: true|false`.

| Status | Code | When | Retry? |
|---|---|---|---|
| 400 | `IDEMPOTENCY_KEY_REQUIRED` | header missing or blank | fix the request |
| 400 | `VALIDATION_ERROR` | malformed body | fix the request |
| 402 | `PAYMENT_DECLINED` | payment failed. Nothing was claimed. | yes, after fixing payment |
| 404 | `CART_NOT_FOUND` | | no |
| 409 | `CART_ALREADY_CHECKED_OUT` | a *different* key already checked this cart out (`details.order_id`) | no. Fetch the order. |
| 409 | `INSUFFICIENT_INVENTORY` | some line exceeds stock (`details.items[]` with `requested` and `available`) | after editing the cart |
| 409 | `PRICE_CHANGED` | `expected_subtotal_cents` differs from the current subtotal (`details` shows old and new prices) | after review |
| 409 | `COUPON_ALREADY_REDEEMED` | coupon already used | without the coupon |
| 422 | `CART_EMPTY` | | after adding items |
| 422 | `COUPON_NOT_FOUND` | unknown code | no |
| 422 | `IDEMPOTENCY_KEY_REUSED` | key already used for a different cart or body | use a new key |
| 503 | `SERVICE_BUSY` | database lock wait timed out (`Retry-After`) | yes, same key |

Failed checkouts change nothing: no stock, coupon or cart state is touched, and the failure
is not recorded against the idempotency key. Retrying with the same key re-runs the checkout.

#### `GET /orders/{orderId}`
**200** returns the order. It is built only from the order's own snapshot, so later catalog
changes do not alter it.

```json
{
  "id": "ord_…", "order_number": 7, "status": "placed", "cart_id": "cart_…", "currency": "USD",
  "lines": [{ "product_id": "sticker-pack", "product_name": "Sticker Pack",
              "unit_price_cents": 333, "quantity": 5, "line_total_cents": 1665 }],
  "subtotal_cents": 1665,
  "coupon": { "code": "SAVE10-ABCDEFGHJK", "percent_off": "10.00" },
  "discount_cents": 167, "total_cents": 1498,
  "payment_reference": "fakepay_…", "created_at": "…"
}
```
`order_number` is the order's position in the sequence of placed orders, which coupon
milestones count. `coupon` is `null` when no coupon was used. `payment_reference` is `null`
when the total is 0. Errors: **404** `ORDER_NOT_FOUND`.

#### `PATCH /admin/products/{productId}` (admin)
Body: `{"unit_price_cents": 3499, "stock": 10}`. Either field is optional, and `stock` is absolute.
This exists so you can test what happens when price or availability changes before checkout.
Returns **200** with the product. Errors: **400** `VALIDATION_ERROR`, **404** `PRODUCT_NOT_FOUND`.

#### `POST /admin/coupons` (admin)
No body. Generates **one** coupon for the oldest milestone (n, 2n, 3n, … placed orders)
that has been reached but not yet rewarded.

**201** response:
```json
{ "coupon": { "code": "SAVE10-ABCDEFGHJK", "milestone_order_number": 5, "percent_off": "10.00",
              "status": "available", "created_at": "…", "redeemed_order_id": null, "redeemed_at": null },
  "remaining_pending_milestones": 0 }
```
**409** `NO_ELIGIBLE_MILESTONE` means there is nothing to reward. `details` includes
`placed_orders` and `next_milestone_at`. Repeating the request never creates a second coupon
for the same milestone.

#### `GET /admin/coupons` (admin)
**200** returns `{"coupons": [...], "milestones": {"every_n_orders", "percent_off", "placed_orders", "eligible_milestones", "rewarded_milestones", "pending_milestones": [10], "next_milestone_at"}}`.

#### `GET /admin/orders` (admin)
**200** returns `{"orders": [<order>, ...]}` ordered by `order_number`. The report can be reconciled against this list.

#### `GET /admin/report` (admin)
Read-only. All figures come from one database snapshot, and repeated calls return identical bodies while nothing else changes.

```json
{
  "currency": "USD",
  "orders_placed": 12,
  "products": [{ "product_id": "keyboard", "name": "Mechanical Keyboard", "quantity_sold": 3, "gross_revenue_cents": 26997 }],
  "revenue": { "gross_cents": 98765, "discounts_cents": 1234, "net_cents": 97531 },
  "coupons": { "generated": 2, "available": 1, "redeemed": 1 },
  "milestones": { "every_n_orders": 5, "percent_off": "10.00", "eligible": 2, "rewarded": 2, "pending": 0, "next_milestone_at": 15 }
}
```
How the figures reconcile:
- `gross_cents` = Σ order `subtotal_cents` = Σ product `gross_revenue_cents`
- `discounts_cents` = Σ order `discount_cents`
- `net_cents` = Σ order `total_cents` = gross − discounts
- `coupons.redeemed` = number of orders with a coupon

### Example session (curl)

```bash
CART=$(curl -s -X POST localhost:8000/carts | python -c "import sys,json;print(json.load(sys.stdin)['id'])")
curl -s -X PUT localhost:8000/carts/$CART/items/keyboard -H 'Content-Type: application/json' -d '{"quantity":2}'
curl -s localhost:8000/carts/$CART
curl -s -i -X POST localhost:8000/carts/$CART/checkout -H 'Idempotency-Key: 7d1c…' -H 'Content-Type: application/json' -d '{}'
curl -s -i -X POST localhost:8000/carts/$CART/checkout -H 'Idempotency-Key: 7d1c…' -H 'Content-Type: application/json' -d '{}'   # replay -> 200
curl -s -X POST localhost:8000/admin/coupons
curl -s localhost:8000/admin/report
```

## Layout

```
app/
  api.py        HTTP routes, request models, error envelope
  db.py         schema (constraints back the invariants), seed, transaction helpers
  carts.py      cart editing and pricing view
  checkout.py   checkout use case (idempotency, stock, coupon, payment, order snapshot)
  coupons.py    milestone computation and coupon generation
  orders.py     order views
  reporting.py  admin summary
  money.py      integer-cent and basis-point arithmetic
  payments.py   payment seam with a fake gateway
tests/          live-server tests (concurrency, idempotency, coupon rules, money)
scripts/demo.py executable walkthrough
```
