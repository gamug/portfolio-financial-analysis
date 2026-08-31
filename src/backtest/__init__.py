"""Roadmap step 9: backtest harness -- PENDING, not yet implemented.

Reserved shape (no new production data -- reads what the other steps wrote):

- inputs: ``portfolio_position`` history + ``price_observation``
- outputs: time-weighted returns vs SPX, turnover, drawdown, per-``score_type``
  attribution
- tables (sketch): ``backtest_run``, ``backtest_return`` (per-date NAV),
  ``backtest_metric``

Depends on Phases 4 + 6 having produced ``portfolio_position`` history across
many ``cycle_date`` values. Deferred to a future branch.
"""

from __future__ import annotations

__all__: list[str] = []
