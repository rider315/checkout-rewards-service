# Design decisions

The short version: **every state change is one short database transaction that holds the
SQLite write lock for its whole read-check-write sequence**. Each invariant is also backed by
a database constraint or a guarded `UPDATE`, so a bug in the locking would surface as a
rejected write, not as corrupted data. Idempotency is keyed on a client-supplied
`Idempotency-Key` that is stored on the order in the same transaction that creates it.

---

## 1. Invariants

| # | Invariant | Primary mechanism | Backstop | Test |
|---|---|---|---|---|
| I1 | Stock never goes below zero, so the store never sells more than it has | checkout claims stock under the write lock after checking every line | `UPDATE … SET stock = stock - q WHERE stock >= q` with a rowcount check, plus `CHECK (stock >= 0)` | `test_concurrent_checkouts_never_oversell` |
| I2 | A cart produces at most one order | cart status `open → checked_out` changes inside the checkout transaction | `orders.cart_id UNIQUE`, plus a guarded `UPDATE carts … WHERE status='open'` | `test_concurrent_checkouts_of_one_cart_with_different_keys` |
| I3 | Retrying a checkout with the same key never creates a second order or claims stock twice | key lookup happens first, inside the write transaction | `orders.idempotency_key UNIQUE` | `test_concurrent_retries_with_same_key_place_exactly_one_order` |
| I4 | A coupon is redeemed at most once | status check plus claim inside the checkout transaction | guarded `UPDATE coupons … WHERE status='available'`, plus `orders.coupon_code UNIQUE` | `test_coupon_cannot_be_redeemed_by_two_concurrent_checkouts` |
| I5 | A failed checkout consumes nothing: no stock, no coupon, cart stays open | all claims happen in one transaction, and any exception rolls it back | none needed, because atomicity covers it | `test_failed_checkout_does_not_consume_coupon`, `test_payment_decline_rolls_back_stock_coupon_and_cart` |
| I6 | At most one coupon per milestone, and only for milestones that have been reached | generation runs under the write lock | `coupons.milestone_order_number UNIQUE` | `test_concurrent_coupon_generation_rewards_each_milestone_once` |
| I7 | An order explains itself forever | order lines snapshot the name, unit price and quantity. The order stores subtotal, discount, percentage and coupon. | `CHECK (line_total = unit_price * quantity)`, `CHECK (total = subtotal - discount)`, `CHECK (discount <= subtotal)` | `test_order_keeps_its_price_snapshot_when_catalog_changes` |
| I8 | Totals are exact and never negative | integer cents and integer basis points only, with the discount capped at the subtotal | the CHECK constraints above | `test_money.py`, `test_full_discount_yields_zero_total_without_charging` |
| I9 | The report reconciles with orders and coupons, and reading it changes nothing | it is computed from one read snapshot and has no write path | n/a | `test_mixed_concurrent_workload_reconciles` |
| I10 | Invalid products and quantities never enter a cart | strict request models plus domain validation inside the write transaction | `CHECK (quantity > 0)`, foreign key to `products` | `test_invalid_quantities_never_enter_the_cart` |

I checked that the tests can catch broken implementations by temporarily mutating the code:

- **Replacing `BEGIN IMMEDIATE` with a deferred `BEGIN`** made 5 of the 6 concurrency tests
  fail. The oversell test still passed, because the guarded stock `UPDATE` (I1's backstop)
  rejected the losing writes on its own. That is the reason for the defence in depth.
- **Removing the coupon status checks and the `UNIQUE` constraint** made the coupon race test fail.

## 2. Ambiguities and the semantics I chose

| Question | Choice |
|---|---|
| What happens when the price changes between add-to-cart and checkout? | Checkout charges the **current** price. The cart view flags `price_changed` per line and shows the price at add time. The client can pass `expected_subtotal_cents`, and checkout then fails with `409 PRICE_CHANGED` instead of charging a different amount. See D3. |
| What happens when stock drops below a cart quantity before checkout? | The cart is not modified behind the customer's back. The view shows `sufficient_stock: false` and `checkout_ready: false`. Checkout fails with `409 INSUFFICIENT_INVENTORY`, listing `requested` and `available` per line. There are **no partial orders**: either every line is fulfilled or nothing is. |
| Does adding to a cart reserve stock? | No. Add and update reject quantities above current stock to give fast feedback, but stock is claimed only at checkout. See D4. |
| What does "a coupon becomes available every nth order" mean? | Milestones are placed-order counts n, 2n, 3n, …. A milestone is *eligible* once that many orders exist. An admin request creates one coupon for the oldest eligible milestone that has no coupon yet. |
| Several milestones are pending (for example, the admin didn't call for a while) | Each call rewards **one** milestone, oldest first, and the response reports `remaining_pending_milestones`. Milestones never expire, and calling repeatedly catches up. |
| Who can use a coupon? | There is no customer identity in scope, so a coupon is a **bearer code** that the admin distributes. It records `milestone_order_number`, so the admin can see which order earned it (and therefore whom to send it to). |
| Do orders that used a coupon count toward the next milestone? | Yes. Every successfully placed order counts. Tested. |
| What does the discount apply to? | The order subtotal, at most one coupon per order, no stacking. The discount is `round_half_up(subtotal × x%)`, capped at the subtotal. |
| What does a coupon's percentage come from? | It is snapshotted from config when the coupon is generated, so changing `x` later never alters coupons that were already issued, and orders store their own `discount_bp`. |
| Do coupons expire? | No. Expiry is deferred. |
| What if *n* changes on an existing database? | Not supported. Milestones are stored as absolute order counts, and a different `n` would give a different eligible set. In production the rule would be versioned. Documented as deferred. |
| What if a checkout retry has the same key but a different coupon or cart? | `422 IDEMPOTENCY_KEY_REUSED`. A key binds to (cart, coupon, expected subtotal). |
| Are failed checkouts replayed for the same key? | No. Only successes are stored. A failure changes no state, so re-running it is safe and lets a retry succeed after the customer fixes the cause. See D2. |
| Can a cart be edited after checkout? | No. `409 CART_NOT_OPEN`. |
| Can an order be cancelled or refunded? | Out of scope. Orders have a single status, `placed`. |
| Does a 100% coupon still call the payment gateway? | No. A zero total skips the gateway and the order's `payment_reference` is `null`. |
| Does deleting an item that isn't in the cart succeed? | No, it returns `404 ITEM_NOT_IN_CART`. The *effect* is still idempotent, because the item is gone either way. |

## 3. Material decisions

### D1. Persistence and concurrency control: SQLite with whole-use-case write transactions
- **Context:** Every interesting race is a read-check-write: is there stock, is the coupon
  unused, is the cart open. The check and the write must be atomic with respect to other
  requests, including requests handled by other processes.
- **Options considered:**
  1. In-memory store with a process-wide mutex. Simple, but the guarantee disappears with a
     second process, and atomic rollback has to be hand-written.
  2. SQLite with deferred transactions plus optimistic checks (guarded `UPDATE`s / version
     columns), retrying on conflict.
  3. **SQLite with `BEGIN IMMEDIATE`**, which takes the database-wide write lock before the
     first read, plus guarded `UPDATE`s and constraints as a backstop.
  4. Postgres with row locks. This is the right production answer, but it needs Docker or a
     server, which hurts "repeatable setup with no private services".
- **Choice:** Option 3.
- **Why:** It gives real ACID rollback, durability, and serialization that works **across
  processes** (`uvicorn --workers 4` is safe, and I ran the demo that way). Each use case's
  logic reads as straight-line code. In WAL mode a deferred transaction that later tries to
  write fails immediately with `SQLITE_BUSY` if another writer committed in between, which
  would surface as spurious 503s. `IMMEDIATE` queues writers instead (`busy_timeout` is 15 s).
  The mutation test confirmed this: with deferred `BEGIN`, the concurrency tests failed with
  busy errors.
- **Consequences:** All writes are serialized globally. That is fine at this scale because the
  transactions are a few milliseconds long, but it is a throughput ceiling (see §9). Reads do
  not block, because WAL lets them see a snapshot. I kept the guarded `UPDATE`s even though
  they are redundant under the global lock, because they are exactly what a row-locking
  database would rely on, which makes the move to Postgres smaller.

### D2. Idempotency: a required `Idempotency-Key`, stored on the order, success-only
- **Context:** A client that timed out cannot know whether the order was placed. A retry must
  return the original result instead of placing a second order or claiming stock again.
- **Options considered:**
  1. Use the cart as the natural key: a second checkout of a checked-out cart returns the existing order.
  2. An optional key, falling back to option 1.
  3. **A required key** stored with a fingerprint of the request on the order row.
  4. A separate idempotency table that records *every* response, including failures, with an
     `in_progress` state (the Stripe-style approach).
- **Choice:** Option 3, with the one-checkout-per-cart rule as a second, independent guard.
- **Why:** Option 1 can't distinguish "my retry" from "someone else already checked out this
  cart with different terms" (for example, a different coupon), and it gives the client no way
  to tell whether it got a replay or a conflict. A required key makes the contract explicit:
  `201` means placed now, `200 + Idempotent-Replayed: true` means placed earlier, and
  `409 CART_ALREADY_CHECKED_OUT` means a different request won. Storing the key **in the same
  row and transaction as the order** means there is no window in which the order exists but
  the key doesn't, or the reverse. I chose not to record failures (option 4) because a failed
  checkout changes no state, so re-executing it is safe and more useful: after a restock or a
  payment fix, the same key can succeed. Concurrent duplicates are serialized by the write
  lock, so the second one simply sees the stored order.
- **Consequences:** Clients must send a header, and a missing header is a clear `400`. Keys
  never expire (a TTL is deferred). The fingerprint covers (cart, coupon, expected subtotal),
  not the cart contents. If a first attempt *fails*, the customer edits the cart, and the
  client retries with the same key, the edited cart is checked out. That is acceptable because
  the key identifies "check out this cart", and the cart can still only be checked out once.
  In a multi-instance deployment with slow external calls, option 4's `in_progress` state
  becomes necessary (§9).

### D3. Carts reference products, and checkout charges current prices with an optional guard
- **Context:** Prices and stock can change while a cart sits around. Either the store absorbs
  the change, or the customer does, or the customer must confirm it.
- **Options considered:**
  1. Lock the price at add-to-cart time and charge that price. Stale carts can then buy at
     old prices indefinitely, so price locks would need expiry.
  2. Silently charge the current price.
  3. **Charge the current price, make the change visible, and let the client opt in to a hard
     guard** (`expected_subtotal_cents` → `409 PRICE_CHANGED`).
- **Choice:** Option 3.
- **Why:** The catalog stays the single source of truth for price, which keeps the
  ledger simple. The customer is never charged an amount they did not see, as long as the
  client sends the subtotal it displayed. The cart stores `unit_price_cents_at_set` only to
  explain drift (`price_changed`). The guard failure is a `409`, so the client can re-render
  the cart and resubmit.
- **Consequences:** Clients that don't send the guard accept the current price, which is the
  documented default. Setting the quantity again refreshes the "seen" price.

### D4. Stock is claimed only at checkout, with no reservations
- **Context:** "Must not sell more inventory than is available" while carts are long-lived and
  often abandoned.
- **Options considered:** reserve stock when an item is added (needs TTLs and a sweeper, and
  abandoned carts lock up scarce stock); **claim at checkout**.
- **Choice:** Claim at checkout. Add and update check the current stock only to give fast feedback.
- **Why:** It is simpler, and nothing can leak. The race is decided at the one moment that
  matters, inside the checkout transaction. All lines are claimed or none are.
- **Consequences:** A customer can see "in stock" and still lose the race at checkout, and
  gets a precise `INSUFFICIENT_INVENTORY` listing `available`. For flash sales, reservations
  with a short TTL would be the next step.

### D5. Coupons are generated on request for the oldest unrewarded milestone and redeemed inside checkout
- **Context:** The coupon must be issued only once per milestone, redeemed only once, and never
  lost by a failed checkout.
- **Options considered:**
  - For generation: automatically create a coupon inside the checkout that hits a milestone, or
    **create it on an admin request** (as the brief asks). Also: generate all pending
    milestones at once, or **one per call**.
  - For redemption: a separate "apply coupon" step that reserves the coupon on the cart, or
    **claim it inside the checkout transaction**.
- **Choice:** Admin-triggered generation, one per call, oldest first, protected by
  `UNIQUE(milestone_order_number)`. The claim happens inside checkout with a guarded `UPDATE`
  and `orders.coupon_code UNIQUE`.
- **Why:** Generation under the write lock is trivially race-free, and the `UNIQUE`
  constraint makes double issuance impossible even if the lock logic were wrong. One coupon per
  call keeps the operation's result unambiguous: the response is a single coupon. Claiming
  inside checkout means the coupon's fate is tied to the order's fate by atomicity. There is no
  reservation state that could leak if a checkout fails after reserving. Of two concurrent
  checkouts with the same coupon, the loser fails entirely with
  `409 COUPON_ALREADY_REDEEMED` rather than silently proceeding at full price. The customer
  asked for a discount, so they should decide.
- **Consequences:** Order numbers must be dense so milestones are well defined.
  `order_number` = count + 1 under the lock. A Postgres sequence would not work here because it
  has gaps on rollback (§9).

### D6. Money uses integer cents and basis points, rounded half-up once per order
- **Context:** Exact arithmetic, deterministic discounts, never a negative total.
- **Options considered:** floats (rejected), `Decimal` amounts, **integer minor units**. For
  rounding: round down (merchant-favourable), banker's rounding, or **half-up**. For where to
  apply the discount: per line, which then needs an allocation of rounding remainders, or
  **once on the subtotal**.
- **Choice:** All amounts are `int` cents and percentages are `int` basis points. Config
  parsing rejects percentages with more than 2 decimals rather than silently re-rounding them.
  The discount is `(subtotal × bp + 5000) // 10000`, half-up, capped at the subtotal, and
  `total = subtotal − discount`.
- **Why:** Integer arithmetic is exact and identical in Python and SQL. Half-up is the rule
  most people expect ("$0.005 rounds to $0.01"). Applying the discount once means there is
  exactly one rounding step per order, so the result is easy to reproduce from the stored
  numbers.
- **Consequences:** Per-line discount allocation (needed for partial refunds) is deferred. The
  API exposes `*_cents` integers, and clients do the formatting.

### D7. Payment is a seam with a fake gateway, called inside the transaction
- **Context:** No real payment provider is required, but checkout's atomicity should be tested
  against a failure that happens *after* stock and the coupon are claimed.
- **Options considered:** no abstraction (treat success as paid); **a minimal `PaymentGateway`
  protocol with an always-approving fake**; a full reservation-then-payment saga.
- **Choice:** The protocol plus the fake, called as the last step before the order is written.
  A `PaymentDeclined` (`402`) rolls everything back. Tests inject a gateway that declines.
- **Why:** It gives a realistic, late failure point to prove I5 (stock, coupon and cart are all
  restored), at almost no cost.
- **Consequences / caveat:** Calling a payment provider **while holding a database lock is
  wrong in production**. A slow provider would stall every writer. This is acceptable only
  because the fake is instantaneous. §9 describes the production shape.

### D8. Order snapshot: denormalized order lines
- **Context:** An order must keep explaining its total after products are renamed or repriced.
- **Options considered:** reference products and keep a price history table; **copy the name,
  unit price, quantity and line total into `order_lines`, and the subtotal, discount, percentage
  and coupon into `orders`**.
- **Choice:** The snapshot. `GET /orders/{id}` never reads `products`.
- **Why:** It is the simplest way to make orders immutable facts. The CHECK constraints make
  the stored arithmetic self-verifying.
- **Consequences:** Some duplicated data. The report uses order lines, so it is also
  unaffected by catalog edits.

### D9. Item quantities are set with an absolute `PUT` instead of an incrementing `POST`
- **Context:** Cart edits are retried too, and "add 1" executed twice becomes 2.
- **Options considered:** `POST /items` with increment semantics; **`PUT /items/{productId}`
  with an absolute quantity**.
- **Choice:** `PUT` handles both "add" (`201`) and "change quantity" (`200`).
- **Why:** It is naturally idempotent, so no key is needed for cart edits.
- **Consequences:** Clients send the desired quantity, not a delta.

### D10. Error model: stable codes, coarse status classes, first failing check wins
See §6.

## 4. Transactions, concurrency and idempotency

Checkout runs in one `BEGIN IMMEDIATE` transaction (see [app/checkout.py](app/checkout.py)):

1. Look up the `Idempotency-Key`. If it was found with the same fingerprint, **replay** (`200`).
   If it was found with a different fingerprint, `422`.
2. Check that the cart exists and is `open`. Otherwise `404` or `409 CART_ALREADY_CHECKED_OUT`.
3. Load the lines at current prices. Empty → `422`. Any line over stock → `409` listing every short line.
4. If an `expected_subtotal_cents` guard was sent and doesn't match → `409 PRICE_CHANGED`.
5. Check that the coupon exists and is available, and compute the discount.
6. Claim stock with guarded `UPDATE`s, ordered by product id.
7. Claim the coupon with a guarded `UPDATE`.
8. Close the cart with a guarded `UPDATE`.
9. Charge through the gateway. A decline raises.
10. Insert the order (with the key and fingerprint) and its lines, then commit.

Any exception at any step rolls back steps 6–10 together.

How each race resolves:

| Race | Outcome | Why |
|---|---|---|
| N checkouts for the last k units | exactly k succeed; the rest get `409 INSUFFICIENT_INVENTORY` with nothing claimed | they serialize on the lock, and the guarded `UPDATE` is the backstop |
| Same request retried concurrently (same key) | one `201`, the rest `200` with the same order | the key is checked first inside the lock |
| Same cart, different keys | one `201`, the rest `409 CART_ALREADY_CHECKED_OUT` with `order_id` | cart status check, plus `UNIQUE(cart_id)` |
| Two carts, same coupon | one gets the discount, the other `409 COUPON_ALREADY_REDEEMED` and claims no stock | guarded `UPDATE`, plus `UNIQUE(coupon_code)` |
| Concurrent coupon generation | one coupon per eligible milestone, the rest `409 NO_ELIGIBLE_MILESTONE` | lock, plus `UNIQUE(milestone)` |
| Cart edit racing checkout | the edit happens entirely before (and is included) or after (and gets `409 CART_NOT_OPEN`) | both are write transactions |
| Report during writes | it sees a consistent before-or-after state | WAL snapshot inside one read transaction |
| Lock wait exceeds 15 s | `503 SERVICE_BUSY` with `Retry-After`. Safe to retry with the same key. | |

## 5. Money and rounding rules
- The only representation is integer minor units (`*_cents`), with one currency (USD) per deployment.
- `line_total = unit_price × quantity`, and `subtotal = Σ line_total`.
- `discount = min(subtotal, ⌊(subtotal × bp + 5000) / 10000⌋)`. This rounds half-up to the
  nearest cent, is applied once per order and is never per line.
- `total = subtotal − discount ≥ 0`. All of this is enforced by CHECK constraints as well as in code.
- Worked examples: 10% of 333 = 33.3 → **33**. 10% of 1665 = 166.5 → **167**. 12.5% of 999 =
  124.875 → **125**. 100% of 24999 → **24999**, so the total is **0** and payment is skipped.

## 6. Error model
- **Envelope:** `{"error": {"code", "message", "details"}}` on every error, including unknown
  routes (`ROUTE_NOT_FOUND`) and malformed JSON (`VALIDATION_ERROR` with a per-field `problems` list).
- **`code` is the contract,** and `message` is for humans. Codes are specific enough for a
  client to act on without parsing text. For example, `INSUFFICIENT_INVENTORY` carries
  `requested` and `available` per product, and `CART_ALREADY_CHECKED_OUT` carries the `order_id`.
- **The status code gives the broad class:**
  - `400` means the request itself is wrong.
  - `402` means payment failed.
  - `404` means the resource in the *path* doesn't exist.
  - `409` means the request is valid but conflicts with current state, and could succeed after the state changes.
  - `422` means the request is well-formed but references something unusable (an unknown
    product or coupon in the *body*, an empty cart, a reused key).
  - `503` means a transient failure, so retry.
- **Unknown product in the body is `422`, not `404`.** That keeps 404 meaning "this URL's
  resource doesn't exist", which clients often treat specially.
- **The first failing check wins.** The order is: key, cart, empty, stock, price guard, coupon.
  A cart that is both out of stock and using a used coupon reports the stock problem first.
  Every short line is reported together.
- Unexpected exceptions return `500 INTERNAL_ERROR` without leaking internals, and the
  transaction is rolled back.

## 7. Implemented vs. deferred

**Implemented:** everything in the brief. That covers the catalog with seed data and a limited
product, the full cart lifecycle with validation, idempotent checkout, order snapshots,
coupon generation and redemption, the report, and an admin price/stock endpoint for exercising
price and availability changes. Also: a fake payment seam, an `/admin/orders` and
`/admin/coupons` listing for reconciliation, live-server concurrency tests, and an executable
demo.

**Deliberately deferred:**
- Authentication and authorization. `/admin/*` is the boundary where an admin role check would go.
- Customer identity, so coupons are bearer codes not bound to a customer.
- An expiry/TTL for idempotency keys, carts and coupons, plus cleanup jobs.
- Stock reservations, order cancellation/refunds, and per-line discount allocation.
- Pagination on `/admin/orders` and `/admin/coupons`.
- Product creation, deactivation and deletion. Products are seeded, and only price and stock are editable.
- Versioned coupon rules (changing `n` safely), multiple currencies, and taxes or shipping.
- Rate limiting, structured logging and metrics.

## 8. Testing approach
The tests target the failure modes, not the endpoints. Every concurrency test runs against
a **real uvicorn server** with a file-backed database. It releases 8–35 requests at the same
instant with a barrier and then asserts invariants rather than a particular interleaving. The
mixed-workload test reconciles the report against `/admin/orders`, `/admin/coupons` and stock
deltas, and checks that two report reads are identical. Mutation checks (§1) showed the suite
fails when the locking or the coupon guards are removed.

## 9. Evolving to multiple instances and production scale

The current design already survives multiple **processes on one host**, because the lock
lives in the database file. Across hosts I would move to **Postgres** and change the following:

- **Locking granularity.** Replace the global write lock with row-level locking. The stock
  claim becomes `UPDATE products SET stock = stock - $q WHERE id = $id AND stock >= $q`,
  checking the rowcount and running in product-id order to avoid deadlocks (or
  `SELECT … FOR UPDATE` in that order). The coupon claim stays a guarded `UPDATE`. All the
  `UNIQUE` and `CHECK` constraints carry over unchanged and remain the last line of defence.
  `READ COMMITTED` is enough with guarded updates. Report queries run in a
  `REPEATABLE READ` transaction to get one snapshot.
- **Order numbering and milestones.** Sequences have gaps, so milestones should count placed
  orders, not ids. I would add a single-row `order_counter` that is incremented in the
  checkout transaction. That row is a hotspot. If it becomes the bottleneck, the next step is
  to derive milestones asynchronously from an append-only order log, which works because coupon
  generation is admin-triggered and not latency-critical.
- **Idempotency.** Insert the key into an `idempotency_keys` table with status
  `in_progress` *first*, in its own transaction, with `UNIQUE` on the key. A concurrent
  duplicate then gets `409 REQUEST_IN_PROGRESS` (retry later) instead of waiting on a lock.
  Store the final response, and expire keys after something like 24 hours.
- **Payments.** These must come out of the database transaction. The shape would be:
  1. In one transaction, claim stock and the coupon and create the order as `PENDING_PAYMENT`.
  2. Call the provider *outside* any transaction, passing our idempotency key so the provider
     deduplicates too.
  3. In a second transaction, mark the order `PLACED`. On a decline, release the stock and the
     coupon, which is a compensation.
  4. Run a sweeper or provider webhooks for orders stuck in `PENDING_PAYMENT`.
  
  Report figures and milestones would count only `PLACED` orders. An outbox table would
  publish order events reliably.
- **Hot products.** A single scarce SKU serializes on its row. If that hurts (flash sales),
  options are short-TTL reservations, or splitting stock into N buckets and claiming from any
  bucket.
- **Reads and reporting.** Serve product and cart reads from replicas where staleness is
  acceptable. The cart view is advisory anyway. Maintain report aggregates incrementally in
  the checkout transaction, or in a materialized view, instead of scanning all orders.
- **Operations.** Instances become stateless, behind a load balancer. Add connection pooling,
  migrations (for example Alembic) instead of `CREATE IF NOT EXISTS`, and metrics on lock
  waits, `409` rates per code, and replay rates.

## 10. With another two hours I would examine first
1. **An idempotency `in_progress` state and key TTL.** This is the first thing that breaks
   once payment moves outside the transaction.
2. **The pending-payment saga** (§9) with a fake gateway that can time out *after* charging.
   That is the hardest real-world retry case: the customer was charged but we never learned
   about it.
3. **Property-based or randomized model testing.** Generate random interleavings of cart
   edits, admin stock and price changes, checkouts and coupon generation, and compare against a
   simple sequential model after each run.
4. **Contention behaviour under load.** Measure p99 latency of checkout with 50+ concurrent
   writers on SQLite, to quantify the global-lock ceiling claimed in D1.
5. **Coupon rule versioning,** so changing `n` or `x` on a live store has defined semantics.
