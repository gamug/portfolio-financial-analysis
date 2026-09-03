"""HTTP routers, one module per resource. All read-only."""

from __future__ import annotations

from api.routers import health, portfolio, runs, scores, universe

ALL = (health.router, runs.router, universe.router, scores.router, portfolio.router)
