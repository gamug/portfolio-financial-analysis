"""Roadmap step 6/8: Strands-driven selection & monitoring cycles.

Produces TECHNICAL / QUANTITATIVE ``ScoreSnapshot`` rows (FUNDAMENTAL via the
existing analyst, SEMANTIC read-only from the integration repo), evaluates the
``rule_catalog`` into ``veto`` rows with a T-1 contagion lag, ranks the universe,
and (for a selection cycle) writes ``portfolio_position`` targets.
"""

from __future__ import annotations

__all__ = ["run_monitoring", "run_selection"]

from cycle.orchestrator import run_monitoring, run_selection
