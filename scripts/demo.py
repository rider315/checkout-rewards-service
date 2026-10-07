"""Executable walkthrough of the API against a running server.

    python scripts/demo.py [base_url]        (default http://127.0.0.1:8000)

It mutates the store (places orders, resets the limited product's stock), so point it at
a scratch database. Every request and response status is printed.
"""
import json
import sys
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
http = httpx.Client(base_url=BASE, timeout=30)


def show(title, r):
    print(f"\n== {title}\n{r.request.method} {r.request.url.path} -> {r.status_code}")
    print(json.dumps(r.json(), indent=2)[:1500])


def new_cart(items):
    cart_id = http.post("/carts").json()["id"]
    for product_id, quantity in items.items():
        http.put(f"/carts/{cart_id}/items/{product_id}", json={"quantity": quantity}).raise_for_status()
    return cart_id


def checkout(cart_id, key=None, **body):
    return http.post(f"/carts/{cart_id}/checkout", headers={"Idempotency-Key": key or str(uuid.uuid4())}, json=body or None)


def race(fn, args):
    barrier = threading.Barrier(len(args))
    with ThreadPoolExecutor(len(args)) as pool:
        return list(pool.map(lambda a: (barrier.wait(), fn(a))[1], args))


show("Catalog", http.get("/products"))

cart = new_cart({"keyboard": 1, "sticker-pack": 3})
show("Cart with live prices and totals", http.get(f"/carts/{cart}"))
show("Invalid quantity is rejected", http.put(f"/carts/{cart}/items/mouse", json={"quantity": 0}))

key = str(uuid.uuid4())
show("Checkout", checkout(cart, key))
r = checkout(cart, key)
show(f"Retry with the same Idempotency-Key (Idempotent-Replayed: {r.headers['Idempotent-Replayed']})", r)
show("Second checkout of the same cart with a new key", checkout(cart))

http.patch("/admin/products/limited-headphones", json={"stock": 3}).raise_for_status()
carts = [new_cart({"limited-headphones": 1}) for _ in range(10)]
results = race(checkout, carts)
print("\n== 10 simultaneous checkouts for a product with stock 3")
print(dict(Counter(f"{r.status_code} {r.json().get('error', {}).get('code', 'created')}" for r in results)))
print("stock now:", http.get("/products/limited-headphones").json()["stock"])

print("\n== Placing orders until a coupon milestone is eligible")
while True:
    r = http.post("/admin/coupons")
    if r.status_code == 201:
        break
    print("  ", r.json()["error"]["code"], r.json()["error"]["details"])
    checkout(new_cart({"usb-cable": 1})).raise_for_status()
show("Coupon generated", r)
code = r.json()["coupon"]["code"]

results = race(lambda c: checkout(c, coupon_code=code), [new_cart({"mouse": 1}) for _ in range(5)])
print("\n== 5 simultaneous checkouts sharing one coupon")
print(dict(Counter(f"{r.status_code} {r.json().get('error', {}).get('code', 'created')}" for r in results)))

show("Admin report", http.get("/admin/report"))
