"""The input versions a ``quant`` run was built on (T-090).

A risk model and the books optimized from it are keyed by version strings
(``quant_risk_model.model_version``, ``quant_portfolio.engine_version``). Those strings used
to be constants, so re-running over different inputs -- a newer ``fundamental_metrics`` engine,
a new return engine -- silently overwrote the earlier result. The **manifest** names the inputs
the run actually reads; its short tag is folded into those version strings, so:

- the same inputs give the same tag, and a re-run updates the same rows in place;
- different inputs give a different tag, and the run writes *parallel* rows next to the first,
  so two runs can coexist and ``quant evaluate`` (which evaluates every book in its window)
  compares them.

What ``quant`` reads: the ``valuation`` metric group (market capitalisation, the weights behind
the equilibrium expected return) and the return series named by ``return_engine_version``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from portfolio_common.db import Database

from kg_schema.versions import (
    MetricVersions,
    manifest_tag,
    parse_metric_selection,
    resolve_metric_versions,
)
from quant.config import QuantSettings

# The metric groups quant reads. A selection naming any other group is rejected as unread.
QUANT_METRIC_GROUPS: tuple[str, ...] = ("valuation",)


@dataclass(frozen=True)
class QuantManifest:
    metrics: MetricVersions
    return_engine_version: str

    @property
    def inputs(self) -> dict[str, object]:
        return {
            "consumer": "quant",
            "metrics": self.metrics.manifest(),
            "returns": self.return_engine_version,
        }

    @property
    def tag(self) -> str:
        return manifest_tag(self.inputs)

    def json(self) -> str:
        return json.dumps(self.inputs, sort_keys=True, separators=(",", ":"))

    def tagged(self, base_version: str) -> str:
        """*base_version* with this manifest's tag folded in: ``opt-v1`` -> ``opt-v1+3f9a1c2b``."""
        return f"{base_version}+{self.tag}"


def resolve_quant_manifest(conn: Database, settings: QuantSettings) -> QuantManifest:
    """Resolve *settings*' ``metrics_version`` selection against what is stored.

    Raises :class:`kg_schema.versions.VersionError` when the selection is malformed or names
    a version that is not stored -- before any run row is written."""
    versions = resolve_metric_versions(
        conn, parse_metric_selection(settings.metrics_version), groups=QUANT_METRIC_GROUPS
    )
    return QuantManifest(metrics=versions, return_engine_version=settings.return_engine_version)
