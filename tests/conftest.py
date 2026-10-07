"""Tests run against a real uvicorn server on a temporary SQLite file, so concurrent tests
exercise real HTTP requests overlapping on FastAPI's thread pool and the database lock."""
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import httpx
import pytest
import uvicorn

from app.api import create_app
from app.config import Settings


class LiveServer:
    def __init__(self, app):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self.base_url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._server = uvicorn.Server(uvicorn.Config(app, log_level="warning", ws="none"))
        self._thread = threading.Thread(target=lambda: self._server.run(sockets=[self._sock]), daemon=True)

    def start(self) -> None:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


class Api:
    """Thin client with helpers for the common multi-step setups."""

    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url, timeout=30)

    def new_cart(self, items: dict[str, int]) -> str:
        r = self.http.post("/carts")
        assert r.status_code == 201, r.text
        cart_id = r.json()["id"]
        for product_id, quantity in items.items():
            r = self.http.put(f"/carts/{cart_id}/items/{product_id}", json={"quantity": quantity})
            assert r.status_code in (200, 201), r.text
        return cart_id

    def checkout(self, cart_id: str, key: Optional[str] = None, **body) -> httpx.Response:
        return self.http.post(
            f"/carts/{cart_id}/checkout",
            headers={"Idempotency-Key": key or f"key-{cart_id}"},
            json=body or None,
        )

    def place_order(self, items: dict[str, int], **body) -> dict:
        r = self.checkout(self.new_cart(items), **body)
        assert r.status_code == 201, r.text
        return r.json()

    def stock(self, product_id: str) -> int:
        return self.http.get(f"/products/{product_id}").json()["stock"]

    def set_product(self, product_id: str, **fields) -> None:
        r = self.http.patch(f"/admin/products/{product_id}", json=fields)
        assert r.status_code == 200, r.text

    def generate_coupon(self) -> httpx.Response:
        return self.http.post("/admin/coupons")

    def report(self) -> dict:
        r = self.http.get("/admin/report")
        assert r.status_code == 200, r.text
        return r.json()


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


def run_concurrently(fn: Callable, args: list) -> list:
    """Call fn(arg) for every arg on its own thread, releasing them all at the same instant."""
    barrier = threading.Barrier(len(args))

    def worker(arg):
        barrier.wait()
        return fn(arg)

    with ThreadPoolExecutor(max_workers=len(args)) as pool:
        return list(pool.map(worker, args))


@pytest.fixture
def make_api(tmp_path):
    """Factory: start a server with custom settings / payment gateway; all are stopped at teardown."""
    servers = []

    def factory(every_n: int = 3, percent_bp: int = 1000, gateway=None) -> Api:
        settings = Settings(
            database_path=str(tmp_path / f"store-{len(servers)}.db"),
            coupon_every_n_orders=every_n,
            coupon_discount_bp=percent_bp,
        )
        server = LiveServer(create_app(settings, payment_gateway=gateway))
        server.start()
        servers.append(server)
        return Api(server.base_url)

    yield factory
    for server in servers:
        server.stop()


@pytest.fixture
def api(make_api) -> Api:
    """Default store: a 10% coupon every 3rd order."""
    return make_api()
