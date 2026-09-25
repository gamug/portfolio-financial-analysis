"""The input versions a ``quant`` run was built on (T-090), and how the user steers them (T-093).

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
the equilibrium expected return), the return series named by ``return_engine_version``, and --
for ``optimize`` -- a stored risk model.

**Constraints (T-093).** ``--metrics-version`` also takes ``>=``/``!=``/``latest`` and
combinations, and the return series and the risk model can be constrained the same way
(``--returns-version``, ``--risk-model-version``). No constraint keeps T-090's behaviour
exactly. The constraints *as given* and the profile they came from are recorded in the
manifest JSON but are **not** part of the tag: two spellings that resolve to the same versions
are the same inputs, and update the same rows.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from portfolio_common.db import Database

from kg_schema import queries
from kg_schema.versions import (
    MetricVersions,
    VersionError,
    choose_constrained_versions,
    engine_version_key,
    manifest_tag,
    parse_constraint,
    parse_metric_constraints,
    pick_version,
    recognized_engine_versions,
)
from quant.config import QuantSettings
from quant.db import risk_model_versions_at, version_stats

# The metric groups quant reads. A selection naming any other group is rejected as unread.
QUANT_METRIC_GROUPS: tuple[str, ...] = ("valuation",)

# The engine family of each non-metric input: its versions are ``<family>-v<N>``.
ENGINE_FAMILIES: dict[str, str] = {"returns": "qret", "corpact": "corpact", "risk_model": "rm"}

# The configured risk-model label. A book built from it keeps T-090's key; any other risk model
# is folded into the book's tag, so books from two risk models never overwrite each other.
DEFAULT_RISK_MODEL_VERSION: str = str(QuantSettings.model_fields["risk_model_version"].default)


@dataclass(frozen=True)
class QuantManifest:
    metrics: MetricVersions
    return_engine_version: str
    # optimize only: the risk-model label the books are optimized from (None for build).
    risk_model_version: str | None = None
    # the constraints as given (only the ones set) and the profile they came from.
    constraints: Mapping[str, str] = field(default_factory=dict)
    profile: str | None = None

    @property
    def inputs(self) -> dict[str, object]:
        """What the risk model is built from; its hash is :attr:`tag`."""
        return {
            "consumer": "quant",
            "metrics": self.metrics.manifest(),
            "returns": self.return_engine_version,
        }

    @property
    def tag(self) -> str:
        return manifest_tag(self.inputs)

    @property
    def book_inputs(self) -> dict[str, object]:
        """What a book is optimized from: :attr:`inputs`, plus the risk model when it is not
        the configured default (so every book built before T-093 keeps its key)."""
        rm = self.risk_model_version
        if rm is None or rm == DEFAULT_RISK_MODEL_VERSION:
            return self.inputs
        return {**self.inputs, "risk_model": rm}

    @property
    def book_tag(self) -> str:
        return manifest_tag(self.book_inputs)

    def record(self) -> dict[str, object]:
        """The manifest as recorded: :attr:`book_inputs` (so a non-default risk model is named),
        plus the constraints and profile when set. With none of those it is exactly T-090's
        manifest, so a run without T-093 flags records what it always did."""
        out: dict[str, object] = dict(self.book_inputs)
        if self.constraints:
            out["constraints"] = dict(sorted(self.constraints.items()))
        if self.profile:
            out["profile"] = self.profile
        return out

    def json(self) -> str:
        return json.dumps(self.record(), sort_keys=True, separators=(",", ":"))

    def tagged(self, base_version: str) -> str:
        """*base_version* with this manifest's tag folded in: ``rm-v1`` -> ``rm-v1+3f9a1c2b``."""
        return f"{base_version}+{self.tag}"

    def book_tagged(self, base_version: str) -> str:
        """*base_version* with the book tag folded in: ``opt-v1`` -> ``opt-v1+3f9a1c2b``."""
        return f"{base_version}+{self.book_tag}"


def _constraints_given(settings: QuantSettings, *, optimize: bool) -> dict[str, str]:
    given = {"metrics": settings.metrics_version, "returns": settings.returns_version}
    if optimize:
        given["risk_model"] = settings.risk_model_select
    return {k: v for k, v in given.items() if v}


def resolve_engine_input(stored: list[str], text: str, *, input_name: str) -> str:
    """Resolve the constraint *text* for one non-metric input against the *stored* version
    strings (history the consumer never reads -- ``corpact-v1-derived`` -- is ignored)."""
    family = ENGINE_FAMILIES[input_name]
    key = engine_version_key(family)
    recognized, _ = recognized_engine_versions(stored, family)
    return pick_version(
        recognized,
        parse_constraint(text, key=key),
        key=key,
        what=f"{input_name.replace('_', ' ')} version ({family}-v<N>)",
    )


def resolve_return_engine(conn: Database, settings: QuantSettings) -> str:
    if settings.returns_version is None:
        return settings.return_engine_version
    stored = [s.version for s in version_stats(conn, "returns")]
    return resolve_engine_input(stored, settings.returns_version, input_name="returns")


def resolve_corpact_engine(conn: Database, settings: QuantSettings) -> str | None:
    """The one corporate-action engine ``build-returns`` reads, or ``None`` for today's
    per-asset priority (no ``--corpact-version``)."""
    if settings.corpact_version is None:
        return None
    stored = [s.version for s in version_stats(conn, "corpact")]
    return resolve_engine_input(stored, settings.corpact_version, input_name="corpact")


def resolve_quant_manifest(
    conn: Database, settings: QuantSettings, *, optimize_as_of: str | None = None
) -> QuantManifest:
    """Resolve *settings*' version selections against what is stored.

    Pass *optimize_as_of* for ``optimize``: the manifest then also names the risk model the
    books are optimized from. Raises :class:`kg_schema.versions.VersionError` when a selection
    is malformed or cannot be satisfied -- before any run row is written."""
    metrics = choose_constrained_versions(
        queries.metric_versions_present(conn),
        parse_metric_constraints(settings.metrics_version),
        groups=QUANT_METRIC_GROUPS,
    )
    returns = resolve_return_engine(conn, settings)
    risk_model: str | None = None
    if optimize_as_of is not None:
        data_tag = QuantManifest(metrics=metrics, return_engine_version=returns).tag
        risk_model = _resolve_risk_model(conn, settings, optimize_as_of, data_tag)
    return QuantManifest(
        metrics=metrics,
        return_engine_version=returns,
        risk_model_version=risk_model,
        constraints=_constraints_given(settings, optimize=optimize_as_of is not None),
        profile=settings.version_profile,
    )


def _resolve_risk_model(conn: Database, settings: QuantSettings, as_of: str, data_tag: str) -> str:
    """The risk-model label ``optimize`` reads. No constraint: the configured label (loaded,
    or built when missing, as before). A constraint: the highest label satisfying it among the
    models stored for *as_of* over these exact inputs (``<label>+<data_tag>``)."""
    if settings.risk_model_select is None:
        return settings.risk_model_version
    suffix = f"+{data_tag}"
    stored = [
        v.removesuffix(suffix) for v in risk_model_versions_at(conn, as_of) if v.endswith(suffix)
    ]
    try:
        return resolve_engine_input(stored, settings.risk_model_select, input_name="risk_model")
    except VersionError as exc:
        raise VersionError(
            f"{exc} (risk models are matched for as-of {as_of} over these inputs, manifest "
            f"{data_tag}; run build-risk-model first, or pass --model-version to build one)"
        ) from exc
