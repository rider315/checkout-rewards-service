"""ASGI entry point: `uvicorn app.main:app`. Configuration comes from environment variables
(see README)."""
from .api import create_app

app = create_app()
