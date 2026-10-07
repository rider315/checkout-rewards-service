"""Competing and repeated operations. Each test fires its requests simultaneously (released
by a barrier) against a live server, then checks the invariants that must hold no matter
how the requests interleaved."""
import random
from collections import Counter

from conftest import error_code, run_concurrently

LIMITED = "limited-headphones"  # seeded with stock 3


def test_concurrent_checkouts_never_oversell(api):
    carts = [api.new_cart({LIMITED: 1, "usb-cable": 1}) for _ in range(12)]
    cable_stock = api.stock("usb-cable")

    responses = run_concurrently(api.checkout, carts)

    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 3, 409: 9}
    assert all(error_code(r) == "INSUFFICIENT_INVENTORY" for r in responses if r.status_code == 409)
    assert api.stock(LIMITED) == 0
    # Losing checkouts are all-or-nothing: they did not take the other item on their cart either.
    assert api.stock("usb-cable") == cable_stock - 3
    report = api.report()
    assert report["orders_placed"] == 3
    assert {p["product_id"]: p["quantity_sold"] for p in report["products"]}[LIMITED] == 3
    # Carts that lost the race stay open, so the customer can adjust and retry.
    losers = [c for c, r in zip(carts, responses) if r.status_code == 409]
    assert all(api.http.get(f"/carts/{c}").json()["status"] == "open" for c in losers)


def test_concurrent_retries_with_same_key_place_exactly_one_order(api):
    cart = api.new_cart({LIMITED: 2})

    responses = run_concurrently(lambda _: api.checkout(cart, key="retry-me"), range(10))

    assert sorted(r.status_code for r in responses) == [200] * 9 + [201]
    assert {r.json()["id"] for r in responses} == {responses[0].json()["id"]}
    assert Counter(r.headers["Idempotent-Replayed"] for r in responses) == {"true": 9, "false": 1}
    assert api.stock(LIMITED) == 1  # charged once
    assert api.report()["orders_placed"] == 1


def test_concurrent_checkouts_of_one_cart_with_different_keys(api):
    cart = api.new_cart({"mouse": 1})

    responses = run_concurrently(lambda i: api.checkout(cart, key=f"k{i}"), range(8))

    winners = [r for r in responses if r.status_code == 201]
    losers = [r for r in responses if r.status_code != 201]
    assert len(winners) == 1
    assert all(r.status_code == 409 and error_code(r) == "CART_ALREADY_CHECKED_OUT" for r in losers)
    assert all(r.json()["error"]["details"]["order_id"] == winners[0].json()["id"] for r in losers)
    assert api.report()["orders_placed"] == 1


def test_coupon_cannot_be_redeemed_by_two_concurrent_checkouts(api):
    for _ in range(3):
        api.place_order({"sticker-pack": 1})
    code = api.generate_coupon().json()["coupon"]["code"]
    carts = [api.new_cart({"keyboard": 1}) for _ in range(8)]
    keyboard_stock = api.stock("keyboard")

    responses = run_concurrently(lambda c: api.checkout(c, coupon_code=code), carts)

    winners = [r for r in responses if r.status_code == 201]
    assert len(winners) == 1
    assert winners[0].json()["discount_cents"] == 900  # 10% of 89.99 = 8.999 -> 9.00
    assert all(error_code(r) == "COUPON_ALREADY_REDEEMED" for r in responses if r.status_code != 201)
    assert api.stock("keyboard") == keyboard_stock - 1  # losers claimed no stock
    report = api.report()
    assert report["coupons"] == {"generated": 1, "available": 0, "redeemed": 1}
    assert report["revenue"]["discounts_cents"] == 900


def test_concurrent_coupon_generation_rewards_each_milestone_once(api):
    for _ in range(7):  # every 3rd order -> milestones 3 and 6 are eligible
        api.place_order({"sticker-pack": 1})

    responses = run_concurrently(lambda _: api.generate_coupon(), range(10))

    assert Counter(r.status_code for r in responses) == {201: 2, 409: 8}
    assert all(error_code(r) == "NO_ELIGIBLE_MILESTONE" for r in responses if r.status_code == 409)
    milestones = sorted(r.json()["coupon"]["milestone_order_number"] for r in responses if r.status_code == 201)
    assert milestones == [3, 6]
    assert api.report()["coupons"]["generated"] == 2


def test_mixed_concurrent_workload_reconciles(api):
    """Random checkouts, retries, coupon use and coupon generation all at once; afterwards
    the report must agree with the orders, coupons and stock the API exposes."""
    rng = random.Random(1234)
    products = ["keyboard", "mouse", "sticker-pack", LIMITED]
    initial_stock = {p: api.stock(p) for p in products}
    for _ in range(3):
        api.place_order({"mouse": 1})
    seed_code = api.generate_coupon().json()["coupon"]["code"]

    jobs = []
    for i in range(24):
        cart = api.new_cart({p: rng.randint(1, 2) for p in rng.sample(products, rng.randint(1, 3))})
        coupon = seed_code if i % 6 == 0 else None
        jobs.append(("checkout", cart, coupon))
        if i % 4 == 0:
            jobs.append(("checkout", cart, coupon))  # duplicate delivery of the same request
        if i % 5 == 0:
            jobs.append(("generate", None, None))
    rng.shuffle(jobs)

    def run(job):
        kind, cart, coupon = job
        if kind == "generate":
            return api.generate_coupon()
        return api.checkout(cart, **({"coupon_code": coupon} if coupon else {}))

    responses = run_concurrently(run, jobs)
    allowed = {201, 200, 409}
    assert all(r.status_code in allowed for r in responses), [r.text for r in responses if r.status_code not in allowed]

    orders = api.http.get("/admin/orders").json()["orders"]
    coupons = api.http.get("/admin/coupons").json()["coupons"]
    report = api.report()

    assert report == api.report()  # reading the report changes nothing
    assert report["orders_placed"] == len(orders)
    assert len({o["cart_id"] for o in orders}) == len(orders)  # one order per cart
    assert report["revenue"] == {
        "gross_cents": sum(o["subtotal_cents"] for o in orders),
        "discounts_cents": sum(o["discount_cents"] for o in orders),
        "net_cents": sum(o["total_cents"] for o in orders),
    }
    assert report["revenue"]["gross_cents"] - report["revenue"]["discounts_cents"] == report["revenue"]["net_cents"]
    for o in orders:
        assert o["subtotal_cents"] == sum(l["line_total_cents"] for l in o["lines"])
        assert 0 <= o["total_cents"] == o["subtotal_cents"] - o["discount_cents"]

    sold = Counter()
    for o in orders:
        for l in o["lines"]:
            sold[l["product_id"]] += l["quantity"]
    for p in report["products"]:
        assert p["quantity_sold"] == sold[p["product_id"]]
    for p in products:
        assert api.stock(p) == initial_stock[p] - sold[p] >= 0

    redeemed = [c for c in coupons if c["status"] == "redeemed"]
    orders_with_coupon = [o for o in orders if o["coupon"]]
    assert len(redeemed) == len(orders_with_coupon) <= 1  # only one coupon code was ever offered
    assert report["coupons"] == {
        "generated": len(coupons),
        "available": len(coupons) - len(redeemed),
        "redeemed": len(redeemed),
    }
    assert len({c["milestone_order_number"] for c in coupons}) == len(coupons)
    assert all(c["milestone_order_number"] <= len(orders) and c["milestone_order_number"] % 3 == 0 for c in coupons)
