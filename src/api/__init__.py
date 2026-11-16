"""The HTTP surface over this repo's outputs.

The read endpoints expose the ``v_*`` read-contract views in ``KG_FINANCIAL_DB``
and the point-in-time universe in ``universe.db`` over JSON, every connection
``mode=ro``, so ``portfolio-reports`` / ``portfolio-app`` (and ad-hoc callers) have
a stable network boundary instead of opening the SQLite files directly.

From Work item 2 (T-011) ``api/`` is also the repo's single entry point for
orchestrated and remote runs: the run endpoints (``api/routers/run.py``; constitution
Tech stack #3, SPEC FR-014) sequence the per-package CLIs -- each step a subprocess, so the
API process opens no write-capable database connection. The status route (a read
endpoint) only polls the orchestrator's state.

Run it: ``python -m api`` (or ``uvicorn --factory api.app:create_app``); the CLI
wrapper is ``python -m api orchestrate --analysis-date D``. Access control for the run
endpoints is deferred (SPEC FR-015) -- they are for the dev container / pilot only.
See ``docs/api.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"
