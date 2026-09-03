"""The FastAPI app factory.

``create_app()`` with no args loads :class:`ApiSettings` from the environment;
pass an explicit ``settings`` in tests. Serve with
``uvicorn --factory api.app:create_app`` or ``python -m api``.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from api import __version__
from api.config import ApiSettings
from api.routers import ALL as ROUTERS

API_PREFIX = "/api/v1"


def create_app(settings: ApiSettings | None = None) -> FastAPI:
    settings = settings or ApiSettings.load()
    app = FastAPI(
        title=settings.title,
        version=__version__,
        summary="Read-only HTTP surface over the v_* read-contract views and universe.db.",
        root_path=settings.root_path,
    )
    app.state.settings = settings

    for router in ROUTERS:
        app.include_router(router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    return app
