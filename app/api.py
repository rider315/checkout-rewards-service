"""HTTP layer: request parsing, status codes and the error envelope. Business rules live in
the domain modules; handlers are plain `def`s, so FastAPI runs them on a thread pool and
overlapping requests really do execute concurrently against the database."""
import sqlite3
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import catalog, db
from .config import Settings
from .errors import AppError


def _error(status: int, code: str, message: str, details: Optional[dict] = None, headers=None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details or {}}},
        headers=headers,
    )


class StrictModel(BaseModel):
    # Reject unknown fields and type coercion ("2" or 2.0 for an integer) instead of guessing.
    model_config = ConfigDict(extra="forbid", strict=True)


class ProductUpdate(StrictModel):
    unit_price_cents: Optional[StrictInt] = None
    stock: Optional[StrictInt] = None


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
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

    return app
