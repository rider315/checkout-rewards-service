"""HTTP layer: request parsing, status codes and the error envelope. Business rules live in
the domain modules; handlers are plain `def`s, so FastAPI runs them on a thread pool and
overlapping requests really do execute concurrently against the database."""
import sqlite3
from typing import Iterator, Optional

from fastapi import Body, Depends, FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import carts, catalog, checkout, coupons, db, orders, reporting
from .config import Settings
from .errors import AppError
from .payments import FakePaymentGateway, PaymentGateway


def _error(status: int, code: str, message: str, details: Optional[dict] = None, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
        headers=headers,
    )


class StrictModel(BaseModel):
    # Reject unknown fields and type coercion ("2" or 2.0 for an integer) instead of guessing.
    model_config = ConfigDict(extra="forbid", strict=True)


class SetQuantity(StrictModel):
    quantity: StrictInt


class CheckoutRequest(StrictModel):
    coupon_code: Optional[str] = None
    # Optional guard: if set and the cart now prices differently, checkout fails with
    # PRICE_CHANGED instead of charging an amount the customer did not see.
    expected_subtotal_cents: Optional[StrictInt] = None


class ProductUpdate(StrictModel):
    unit_price_cents: Optional[StrictInt] = None
    stock: Optional[StrictInt] = None


def create_app(settings: Optional[Settings] = None, payment_gateway: Optional[PaymentGateway] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    gateway = payment_gateway or FakePaymentGateway()
    db.init_db(settings.database_path)

    app = FastAPI(
        title="Checkout and Rewards Service",
        description="Carts, idempotent checkout, milestone coupons. Routes under /admin are administrative.",
        version="1.0.0",
    )

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = db.connect(settings.database_path)
        try:
            yield conn
        finally:
            conn.close()

    # ---- error envelope -------------------------------------------------------------

    @app.exception_handler(AppError)
    def handle_app_error(_: Request, exc: AppError):
        headers = {"Retry-After": "1"} if exc.status_code == 503 else None
        return _error(exc.status_code, exc.code, exc.message, exc.details, headers)

    @app.exception_handler(RequestValidationError)
    def handle_validation_error(_: Request, exc: RequestValidationError):
        problems = [
            {"location": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]} for e in exc.errors()
        ]
        return _error(400, "VALIDATION_ERROR", "The request is malformed.", {"problems": problems})

    @app.exception_handler(StarletteHTTPException)
    def handle_http_error(_: Request, exc: StarletteHTTPException):
        code = {404: "ROUTE_NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        return _error(exc.status_code, code, str(exc.detail))

    @app.exception_handler(sqlite3.OperationalError)
    def handle_db_busy(_: Request, exc: sqlite3.OperationalError):
        if "locked" in str(exc) or "busy" in str(exc):
            return _error(503, "SERVICE_BUSY", "The store is busy; retry the request.", headers={"Retry-After": "1"})
        return _error(500, "INTERNAL_ERROR", "Unexpected database error.")

    @app.exception_handler(Exception)
    def handle_unexpected(_: Request, exc: Exception):
        # Includes constraint violations (sqlite3.IntegrityError): those are invariant
        # backstops firing, i.e. a bug, so they are not presented as client errors.
        return _error(500, "INTERNAL_ERROR", "Unexpected server error.")

    # ---- catalog --------------------------------------------------------------------

    @app.get("/products")
    def list_products(conn=Depends(get_conn)):
        return {"products": catalog.list_products(conn)}

    @app.get("/products/{product_id}")
    def get_product(product_id: str, conn=Depends(get_conn)):
        return catalog.get_product(conn, product_id)

    @app.patch("/admin/products/{product_id}")
    def update_product(product_id: str, body: ProductUpdate, conn=Depends(get_conn)):
        return catalog.update_product(conn, product_id, body.unit_price_cents, body.stock)

    # ---- carts ----------------------------------------------------------------------

    @app.post("/carts", status_code=201)
    def create_cart(response: Response, conn=Depends(get_conn)):
        cart = carts.create_cart(conn, settings)
        response.headers["Location"] = f"/carts/{cart['id']}"
        return cart

    @app.get("/carts/{cart_id}")
    def get_cart(cart_id: str, conn=Depends(get_conn)):
        return carts.get_cart(conn, cart_id, settings)

    @app.put("/carts/{cart_id}/items/{product_id}")
    def set_cart_item(cart_id: str, product_id: str, body: SetQuantity, response: Response, conn=Depends(get_conn)):
        cart, created = carts.set_item_quantity(conn, cart_id, product_id, body.quantity, settings)
        response.status_code = 201 if created else 200
        return cart

    @app.delete("/carts/{cart_id}/items/{product_id}")
    def remove_cart_item(cart_id: str, product_id: str, conn=Depends(get_conn)):
        return carts.remove_item(conn, cart_id, product_id, settings)

    # ---- checkout & orders ----------------------------------------------------------

    @app.post("/carts/{cart_id}/checkout", status_code=201)
    def checkout_cart(
        cart_id: str,
        response: Response,
        body: Optional[CheckoutRequest] = Body(default=None),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        conn=Depends(get_conn),
    ):
        body = body or CheckoutRequest()
        order, replayed = checkout.checkout(
            conn,
            settings,
            gateway,
            cart_id=cart_id,
            idempotency_key=idempotency_key,
            coupon_code=body.coupon_code,
            expected_subtotal_cents=body.expected_subtotal_cents,
        )
        response.status_code = 200 if replayed else 201
        response.headers["Location"] = f"/orders/{order['id']}"
        response.headers["Idempotent-Replayed"] = "true" if replayed else "false"
        return order

    @app.get("/orders/{order_id}")
    def get_order(order_id: str, conn=Depends(get_conn)):
        return orders.get_order(conn, order_id)

    @app.get("/admin/orders")
    def list_orders(conn=Depends(get_conn)):
        return {"orders": orders.list_orders(conn)}

    # ---- coupons & reporting (admin) ------------------------------------------------

    @app.post("/admin/coupons", status_code=201)
    def generate_coupon(conn=Depends(get_conn)):
        return coupons.generate_coupon(conn, settings)

    @app.get("/admin/coupons")
    def list_coupons(conn=Depends(get_conn)):
        return coupons.list_coupons(conn, settings)

    @app.get("/admin/report")
    def report(conn=Depends(get_conn)):
        return reporting.build_report(conn, settings)

    return app
