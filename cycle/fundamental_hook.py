"""Adapter slot for running the Strands ``FundamentalAnalyst`` inside a cycle.

Re-scoring a filing on demand needs the fundamental pipeline's fetch+analyze path
exposed as a reusable call; until that refactor lands, the cycle consumes whatever
``score_snapshot`` FUNDAMENTAL rows ``python -m fundamental_agent run`` already
produced. This module returns ``None`` (no hook) so the orchestrator does exactly
that, and is the single place to wire the real analyst in later.
"""

from __future__ import annotations

from cycle.config import CycleSettings
from cycle.orchestrator import FundamentalHook


def make_hook(settings: CycleSettings) -> FundamentalHook | None:
    _ = settings
    return None
