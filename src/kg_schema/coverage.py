"""Data-coverage report shape for a point-in-time universe -- pure business
logic, no SQL (the query layer that fills this in lives in
:mod:`kg_schema.queries`: ``check_coverage`` / ``persist_coverage``).

The S&P 500 universe is now a mutable, dated list (``universe.db``). A run for an
`analysis_date` selects the constituents as of that date -- but the core data
stores (EDGAR filings, pricing, derived observations) are filled incrementally by
prior agent runs, so a member as of ``D`` may have **no** rows to analyse. The
ingesting agents (`fundamental_agent`, `pricing_agent`) fetch their own data and
log a skip; the reading agents (`cycle`, `quant`) silently run on whatever subset
happens to have data.

``SymbolCoverage`` / ``CoverageReport`` make that explicit: per member, does it
have an ``assets`` identity row, a FUNDAMENTAL score, fundamental metrics, daily
prices, enough derived observations, and a total-return series -- all as of ``D``.
The roll-up properties below (``covered``, ``missing``, ``fraction``, ...) are the
only logic in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A member counts as "covered" when every one of these checks passes. `metrics`
# and `returns` are reported but not required (cycle's VALORIZATION uses metrics
# when present; quant's own gate already enforces the return history).
REQUIRED_CHECKS = ("assets", "fundamental", "pricing", "observations")
ALL_CHECKS = ("assets", "fundamental", "metrics", "pricing", "observations", "returns")

DEFAULT_MIN_OBSERVATION_DAYS = 504  # ~2y of trading days; mirrors quant's min_history_days


@dataclass(frozen=True)
class SymbolCoverage:
    symbol: str
    asset_id: int | None
    checks: dict[str, bool]

    @property
    def covered(self) -> bool:
        return all(self.checks[c] for c in REQUIRED_CHECKS)

    @property
    def missing(self) -> list[str]:
        return [c for c in ALL_CHECKS if not self.checks.get(c, False)]

    @property
    def missing_required(self) -> list[str]:
        return [c for c in REQUIRED_CHECKS if not self.checks.get(c, False)]


@dataclass
class CoverageReport:
    as_of: str
    universe: str
    min_observation_days: int
    rows: list[SymbolCoverage] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows)

    @property
    def covered(self) -> int:
        return sum(1 for r in self.rows if r.covered)

    @property
    def fraction(self) -> float:
        return self.covered / self.total if self.total else 1.0

    @property
    def uncovered(self) -> list[SymbolCoverage]:
        return [r for r in self.rows if not r.covered]

    def missing_for(self, check: str) -> list[str]:
        return [r.symbol for r in self.rows if not r.checks.get(check, False)]
