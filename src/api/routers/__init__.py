"""HTTP routers, one module per resource.

The read routers open every database ``mode=ro`` and never start a run. ``run``
(T-011) is the one run router: it starts the orchestrator or a single step; the status
route (``run_status``) is a read router over the orchestrator's state. No read router
imports ``run``.
"""

from __future__ import annotations

from api.routers import (
    contract,
    health,
    portfolio,
    run,
    run_status,
    runs,
    scores,
    universe,
)

ALL = (
    health.router,
    runs.router,
    universe.router,
    scores.router,
    portfolio.router,
    contract.router,
    run.router,
    run_status.router,
)
