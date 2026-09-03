"""A thin, **read-only** HTTP surface over this repo's outputs.

The agents (``fundamental_agent`` / ``pricing_agent`` / ``cycle`` / ``quant`` /
``entity_resolution``) stay CLI-driven and own all writes. This package exposes
the ``v_*`` read-contract views in ``KG_FINANCIAL_DB`` and the point-in-time
universe in ``universe.db`` over JSON, so ``portfolio-reports`` / ``portfolio-app``
(and ad-hoc callers) have a stable network boundary instead of opening the SQLite
files directly.

Run it: ``python -m api`` (or ``uvicorn --factory api.app:create_app``). Nothing
here mutates a database; a "trigger a run" surface, if ever wanted, is a separate,
explicitly-guarded addition -- see ``docs/api.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"
