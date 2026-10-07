"""Business rules and failure modes of carts, checkout and coupons (sequential)."""
import pytest

from app.errors import PaymentDeclined
from conftest import error_code


def _coupon(api) -> str:
    """Reach the first milestone (n = 3) and generate its coupon."""
    for _ in range(3):
        api.place_order({"sticker-pack": 1})
    r = api.generate_coupon()
    assert r.status_code == 201, r.text
    return r.json()["coupon"]["code"]


def _coupon_status(api, code: str) -> str:
    coupons = api.http.get("/admin/coupons").json()["coupons"]
    return next(c["status"] for c in coupons if c["code"] == code)


@pytest.mark.parametrize(
    "body, code",
    [
        ({"quantity": 0}, "INVALID_QUANTITY"),
        ({"quantity": -1}, "INVALID_QUANTITY"),
        ({"quantity": 101}, "INVALID_QUANTITY"),
        ({"quantity": 1.5}, "VALIDATION_ERROR"),
        ({"quantity": "2"}, "VALIDATION_ERROR"),
        ({"quantity": True}, "VALIDATION_ERROR"),
        ({}, "VALIDATION_ERROR"),
        ({"quantity": 1, "price": 1}, "VALIDATION_ERROR"),
    ],
)
def test_invalid_quantities_never_enter_the_cart(api, body, code):
    cart = api.new_cart({})
    r = api.http.put(f"/carts/{cart}/items/mouse", json=body)
    assert r.status_code == 400 and error_code(r) == code
    assert api.http.get(f"/carts/{cart}").json()["items"] == []


def test_unknown_product_and_unavailable_quantity_are_rejected(api):
    cart = api.new_cart({})
    r = api.http.put(f"/carts/{cart}/items/does-not-exist", json={"quantity": 1})
    assert (r.status_code, error_code(r)) == (422, "UNKNOWN_PRODUCT")
    r = api.http.put(f"/carts/{cart}/items/limited-headphones", json={"quantity": 4})
    assert (r.status_code, error_code(r)) == (409, "INSUFFICIENT_INVENTORY")
    assert r.json()["error"]["details"]["items"] == [
        {"product_id": "limited-headphones", "requested": 4, "available": 3}
    ]
    assert api.http.get(f"/carts/{cart}").json()["items"] == []


def test_cart_lifecycle_and_totals(api):
    cart = api.new_cart({"mouse": 2, "sticker-pack": 3})
    assert api.http.put(f"/carts/{cart}/items/mouse", json={"quantity": 1}).status_code == 200
    view = api.http.delete(f"/carts/{cart}/items/sticker-pack").json()
    assert [(i["product_id"], i["quantity"], i["line_total_cents"]) for i in view["items"]] == [("mouse", 1, 2999)]
    assert view["subtotal_cents"] == 2999 and view["checkout_ready"] is True

    r = api.http.delete(f"/carts/{cart}/items/sticker-pack")
    assert (r.status_code, error_code(r)) == (404, "ITEM_NOT_IN_CART")

    assert api.checkout(cart).status_code == 201
    r = api.http.put(f"/carts/{cart}/items/keyboard", json={"quantity": 1})
    assert (r.status_code, error_code(r)) == (409, "CART_NOT_OPEN")


def test_checkout_requires_idempotency_key_and_non_empty_cart(api):
    cart = api.new_cart({})
    r = api.http.post(f"/carts/{cart}/checkout")
    assert (r.status_code, error_code(r)) == (400, "IDEMPOTENCY_KEY_REQUIRED")
    r = api.checkout(cart)
    assert (r.status_code, error_code(r)) == (422, "CART_EMPTY")
    r = api.checkout("cart_missing")
    assert (r.status_code, error_code(r)) == (404, "CART_NOT_FOUND")


def test_idempotency_key_reused_for_a_different_request_is_rejected(api):
    cart = api.new_cart({"mouse": 1})
    first = api.checkout(cart, key="k-1")
    assert first.status_code == 201

    other_cart = api.new_cart({"mouse": 1})
    r = api.checkout(other_cart, key="k-1")
    assert (r.status_code, error_code(r)) == (422, "IDEMPOTENCY_KEY_REUSED")
    r = api.checkout(cart, key="k-1", expected_subtotal_cents=1)
    assert (r.status_code, error_code(r)) == (422, "IDEMPOTENCY_KEY_REUSED")
    assert api.report()["orders_placed"] == 1


def test_failed_checkout_is_not_cached_and_can_be_retried_with_same_key(api):
    cart = api.new_cart({"limited-headphones": 3})
    api.set_product("limited-headphones", stock=2)  # stock drops after the item was added

    view = api.http.get(f"/carts/{cart}").json()
    assert view["checkout_ready"] is False
    assert {"code": "INSUFFICIENT_INVENTORY", "product_id": "limited-headphones"} in view["issues"]

    r = api.checkout(cart, key="same")
    assert (r.status_code, error_code(r)) == (409, "INSUFFICIENT_INVENTORY")
    api.set_product("limited-headphones", stock=5)
    r = api.checkout(cart, key="same")
    assert r.status_code == 201  # the failure was not replayed


def test_failed_checkout_does_not_consume_coupon(api):
    code = _coupon(api)
    cart = api.new_cart({"keyboard": 1, "limited-headphones": 2})
    api.set_product("limited-headphones", stock=1)
    keyboard_stock = api.stock("keyboard")

    r = api.checkout(cart, coupon_code=code)
    assert (r.status_code, error_code(r)) == (409, "INSUFFICIENT_INVENTORY")
    assert _coupon_status(api, code) == "available"
    assert api.stock("keyboard") == keyboard_stock

    api.http.put(f"/carts/{cart}/items/limited-headphones", json={"quantity": 1})
    r = api.checkout(cart, key="second-attempt", coupon_code=code)
    assert r.status_code == 201 and r.json()["coupon"]["code"] == code
    assert _coupon_status(api, code) == "redeemed"


class SwitchableGateway:
    """Approves charges until told to decline. Payment runs after stock and coupon are
    claimed, so a decline is the latest point at which checkout can fail."""

    def __init__(self):
        self.decline = False

    def charge(self, **_):
        if self.decline:
            raise PaymentDeclined("PAYMENT_DECLINED", "The card was declined.")
        return "test-payment"


def test_payment_decline_rolls_back_stock_coupon_and_cart(make_api):
    gateway = SwitchableGateway()
    api = make_api(gateway=gateway)
    code = _coupon(api)
    cart = api.new_cart({"keyboard": 2})
    stock = api.stock("keyboard")
    report_before = api.report()

    gateway.decline = True
    r = api.checkout(cart, key="pay-1", coupon_code=code)
    assert (r.status_code, error_code(r)) == (402, "PAYMENT_DECLINED")
    assert api.stock("keyboard") == stock
    assert _coupon_status(api, code) == "available"
    assert api.http.get(f"/carts/{cart}").json()["status"] == "open"
    assert api.report() == report_before

    gateway.decline = False
    r = api.checkout(cart, key="pay-1", coupon_code=code)
    assert r.status_code == 201 and r.json()["coupon"]["code"] == code
    assert api.stock("keyboard") == stock - 2


def test_order_keeps_its_price_snapshot_when_catalog_changes(api):
    order = api.place_order({"monitor": 1, "sticker-pack": 2})
    report_before = api.report()

    api.set_product("monitor", unit_price_cents=1, stock=0)

    after = api.http.get(f"/orders/{order['id']}").json()
    assert after == order
    assert after["lines"][0] == {
        "product_id": "monitor", "product_name": "27-inch Monitor",
        "unit_price_cents": 24999, "quantity": 1, "line_total_cents": 24999,
    }
    assert api.report()["revenue"] == report_before["revenue"]


def test_price_change_after_adding_is_flagged_and_guardable(api):
    cart = api.new_cart({"mouse": 2})
    seen_subtotal = api.http.get(f"/carts/{cart}").json()["subtotal_cents"]
    api.set_product("mouse", unit_price_cents=3499)

    view = api.http.get(f"/carts/{cart}").json()
    assert view["items"][0]["price_changed"] is True
    assert view["items"][0]["unit_price_cents_when_added"] == 2999
    assert view["subtotal_cents"] == 6998

    r = api.checkout(cart, key="guarded", expected_subtotal_cents=seen_subtotal)
    assert (r.status_code, error_code(r)) == (409, "PRICE_CHANGED")
    assert r.json()["error"]["details"]["current_subtotal_cents"] == 6998
    assert api.report()["orders_placed"] == 0

    # Without the guard (or with the new amount) checkout charges the current price.
    r = api.checkout(cart, key="accepted", expected_subtotal_cents=6998)
    assert r.status_code == 201 and r.json()["total_cents"] == 6998


def test_coupon_discount_is_deterministic_and_single_use(api):
    code = _coupon(api)
    r = api.checkout(api.new_cart({"sticker-pack": 5}), coupon_code=f"  {code.lower()} ")
    assert r.status_code == 201
    order = r.json()
    assert (order["subtotal_cents"], order["discount_cents"], order["total_cents"]) == (1665, 167, 1498)
    assert order["coupon"] == {"code": code, "percent_off": "10.00"}

    r = api.checkout(api.new_cart({"sticker-pack": 1}), coupon_code=code)
    assert (r.status_code, error_code(r)) == (409, "COUPON_ALREADY_REDEEMED")
    r = api.checkout(api.new_cart({"sticker-pack": 1}), coupon_code="NOPE")
    assert (r.status_code, error_code(r)) == (422, "COUPON_NOT_FOUND")


def test_full_discount_yields_zero_total_without_charging(make_api):
    api = make_api(every_n=1, percent_bp=10_000)
    api.place_order({"usb-cable": 1})
    code = api.generate_coupon().json()["coupon"]["code"]

    order = api.place_order({"monitor": 1}, coupon_code=code)
    assert (order["subtotal_cents"], order["discount_cents"], order["total_cents"]) == (24999, 24999, 0)
    assert order["payment_reference"] is None


def test_coupon_generation_requires_an_unrewarded_milestone(api):
    r = api.generate_coupon()
    assert (r.status_code, error_code(r)) == (409, "NO_ELIGIBLE_MILESTONE")
    assert r.json()["error"]["details"]["next_milestone_at"] == 3
    for _ in range(3):
        api.place_order({"usb-cable": 1})
    assert api.generate_coupon().status_code == 201
    r = api.generate_coupon()
    assert (r.status_code, error_code(r)) == (409, "NO_ELIGIBLE_MILESTONE")
    # Orders that used a coupon still count toward the next milestone.
    code = api.http.get("/admin/coupons").json()["coupons"][0]["code"]
    api.place_order({"usb-cable": 1}, coupon_code=code)
    api.place_order({"usb-cable": 1})
    assert api.generate_coupon().status_code == 409
    api.place_order({"usb-cable": 1})
    assert api.generate_coupon().json()["coupon"]["milestone_order_number"] == 6
