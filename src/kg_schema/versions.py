"""Metric-version selection and run manifests (T-090).

``fundamental_metrics`` is append-only per ``engine_version``: a fixed engine writes a
*parallel* row rather than overwriting, so several versions of the same metric accumulate.
Nothing used to choose among them -- a reader that joined the table unfiltered read every
version at once and let row order pick a winner. This module is the one place that decides.

A consumer (``cycle``, ``quant``) **resolves** the versions it will read, per metric group,
from a user selection and what is actually stored:

- no selection -> the newest version present for each group;
- one version (``metrics-v1``) -> that version for every group the consumer reads that has
  any stored rows (a group with none has nothing to select); it is an error if a group has
  *other* versions but not this one, or if no group has it at all;
- per-group overrides (``valuation=metrics-v1,profitability=metrics-v2``) -> those, and the
  newest present for any group not named.

A requested version that is not stored is an **error**, never a silent fallback. The result
is a :class:`MetricVersions`; its ``manifest`` plus a short :func:`manifest_tag` identify the
inputs a run used, so runs over different inputs can coexist and be compared.

Ordering is by an explicit rule, not by ``computed_at``: ``<family>-v<N>`` sorts by the
family's rank in :data:`FAMILY_RANK` and then by ``N``, so ``pre-v1`` < ``metrics-v1`` <
``metrics-v2`` < ``metrics-v10``. A string that does not parse, or whose family is not
registered, is an error rather than a guess.

Passive and behaviour-free like the rest of ``kg_schema``: it reads (via
:func:`kg_schema.queries.metric_versions_present`) and computes; it never writes.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

from portfolio_common.db import Database

from . import queries

# The groups ``fundamental_agent`` writes. Kept here (not imported) because kg_schema is a
# passive shared layer that must not depend on an agent; ``tests/test_metric_versions.py``
# pins it against ``fundamental_agent.metrics`` and the ``valuation`` group.
METRIC_GROUPS: Final[tuple[str, ...]] = (
    "profitability",
    "liquidity",
    "leverage",
    "efficiency",
    "growth",
    "cashflow",
    "roic",
    "cagr",
    "valuation",
)

# ``pre`` = rows migrated in from before versioning existed; ``metrics`` = the versioned engine.
FAMILY_RANK: Final[dict[str, int]] = {"pre": 0, "metrics": 1}

_VERSION_RE = re.compile(r"^(?P<family>.+)-v(?P<n>\d+)$")

# The one filter every ``fundamental_metrics`` reader applies, given the JSON array from
# :meth:`MetricVersions.json_param`. It is a *static* SQL fragment (a single bound
# parameter, no interpolation -- constitution Code & Git #10) and readers alias the table
# ``m``. ``tests/test_metric_versions.py`` fails on any reader that lacks it.
VERSION_FILTER_SQL: Final[str] = (
    "(m.metric_group || '/' || m.engine_version) IN (SELECT value FROM json_each(?))"
)


class VersionError(ValueError):
    """A version string is malformed, or a requested version is not stored."""


def version_key(version: str) -> tuple[int, int]:
    """The sort key of an engine-version string: ``(family rank, N)``."""
    match = _VERSION_RE.match(version)
    if match is None:
        raise VersionError(f"unparseable engine version {version!r}: expected '<family>-v<N>'")
    family = match.group("family")
    if family not in FAMILY_RANK:
        raise VersionError(
            f"unknown engine-version family {family!r} in {version!r}; "
            f"register it in kg_schema.versions.FAMILY_RANK (known: {sorted(FAMILY_RANK)})"
        )
    return FAMILY_RANK[family], int(match.group("n"))


def sort_versions(versions: Iterable[str]) -> list[str]:
    """*versions* oldest to newest, by :func:`version_key`."""
    return sorted(set(versions), key=version_key)


@dataclass(frozen=True)
class MetricVersions:
    """The metrics engine version chosen for each group a consumer reads.

    A group with no stored rows is simply absent -- readers then get nothing for it."""

    by_group: Mapping[str, str]

    def json_param(self) -> str:
        """The bound parameter for :data:`VERSION_FILTER_SQL`."""
        return json.dumps(sorted(f"{g}/{v}" for g, v in self.by_group.items()))

    def manifest(self) -> dict[str, str]:
        """``group -> version``, in a stable order."""
        return dict(sorted(self.by_group.items()))

    def get(self, group: str) -> str | None:
        return self.by_group.get(group)


def parse_metric_selection(text: str | None) -> str | dict[str, str] | None:
    """A user's ``--metrics-version`` text as a selection.

    ``None``/empty -> ``None`` (newest everywhere); ``"metrics-v2"`` -> that string;
    ``"valuation=metrics-v1,profitability=metrics-v2"`` -> a per-group mapping."""
    if text is None or not text.strip():
        return None
    if "=" not in text:
        version = text.strip()
        version_key(version)  # validate now, so a typo fails before any query
        return version
    chosen: dict[str, str] = {}
    for part in (p.strip() for p in text.split(",") if p.strip()):
        group, sep, version = (x.strip() for x in part.partition("="))
        if not sep or not group or not version:
            raise VersionError(f"expected GROUP=VERSION, got {part!r}")
        if group in chosen:
            raise VersionError(f"group {group!r} given more than once")
        version_key(version)
        chosen[group] = version
    return chosen


def choose_versions(
    present: Mapping[str, Iterable[str]],
    selection: str | Mapping[str, str] | None = None,
    *,
    groups: Sequence[str] = METRIC_GROUPS,
) -> MetricVersions:
    """Resolve *selection* against the versions *present* per group (pure; no database).

    Raises :class:`VersionError` for an unknown group, a group the consumer does not read,
    or a requested version that is not stored for a group -- never a silent fallback."""
    unknown = [
        g for g in (selection if isinstance(selection, Mapping) else {}) if g not in METRIC_GROUPS
    ]
    if unknown:
        raise VersionError(f"unknown metric group(s) {unknown}; known: {list(METRIC_GROUPS)}")
    if isinstance(selection, Mapping):
        unread = [g for g in selection if g not in groups]
        if unread:
            raise VersionError(
                f"metric group(s) {unread} are not read by this consumer (it reads {list(groups)})"
            )

    whole = isinstance(selection, str)  # one version for every group the consumer reads
    resolved: dict[str, str] = {}
    for group in groups:
        stored = sort_versions(present.get(group, ()))
        requested = selection.get(group) if isinstance(selection, Mapping) else selection
        if requested is None:
            if stored:
                resolved[group] = stored[-1]
            continue
        version_key(requested)
        if whole and not stored:
            continue  # a group with no rows at all has nothing to select
        if requested not in stored:
            raise VersionError(
                f"metrics version {requested!r} is not stored for group {group!r}; "
                f"stored: {stored or 'none'}"
            )
        resolved[group] = requested
    if isinstance(selection, str) and groups and not resolved:
        raise VersionError(
            f"metrics version {selection!r} is not stored for any group this consumer reads "
            f"({list(groups)}); stored: { {g: sort_versions(v) for g, v in present.items()} or 'none' }"
        )
    return MetricVersions(resolved)


def resolve_metric_versions(
    conn: Database,
    selection: str | Mapping[str, str] | None = None,
    *,
    groups: Sequence[str] = METRIC_GROUPS,
) -> MetricVersions:
    """:func:`choose_versions` against what ``fundamental_metrics`` actually holds."""
    return choose_versions(queries.metric_versions_present(conn), selection, groups=groups)


def manifest_tag(manifest: Mapping[str, object]) -> str:
    """A short, deterministic identifier of a run's input versions (8 hex chars).

    Independent of key order, so the same inputs always give the same tag and a re-run over
    them updates in place, while different inputs give a different one."""
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:8]


# -- version constraints (T-093) -------------------------------------------------------------
#
# T-090's selection is exact: one version, or ``GROUP=VERSION`` pairs. A **constraint** also
# accepts a minimum (``>=metrics-v2``), an exclusion (``!=metrics-v1``) and comma-separated
# combinations of them; it resolves to the *highest* stored version that satisfies every
# clause. ``latest`` (or no text at all) means "the newest stored". A bare version and
# ``=version`` are the same exact clause, so every T-090 form keeps its meaning.
#
# The grammar lives here, next to the ordering rule it relies on; which inputs accept it is
# the consumer's decision (``quant`` does; ``cycle`` keeps T-090's exact selection).

_CLAUSE_RE = re.compile(r"^(?P<op>>=|!=|=)?\s*(?P<version>\S+)$")
_GROUP_CLAUSE_RE = re.compile(r"^(?P<group>[a-z_]+)\s*(?P<rest>(?:>=|!=|=).*)$")
LATEST: Final[str] = "latest"


@dataclass(frozen=True)
class Clause:
    """One comparison: ``op`` is ``=``, ``>=`` or ``!=``."""

    op: str
    version: str

    def __str__(self) -> str:
        return f"{self.op}{self.version}"


@dataclass(frozen=True)
class Constraint:
    """A conjunction of clauses; no clauses means ``latest``."""

    clauses: tuple[Clause, ...] = ()

    @property
    def is_latest(self) -> bool:
        return not self.clauses

    def __str__(self) -> str:
        return ",".join(str(c) for c in self.clauses) or LATEST

    def rejection(self, version: str, key: VersionKey) -> str | None:
        """Why *version* fails this constraint (the first failing clause), or ``None``."""
        for clause in self.clauses:
            if clause.op == "=" and version != clause.version:
                return f"is not {clause.version}"
            if clause.op == "!=" and version == clause.version:
                return f"is excluded by !={clause.version}"
            if clause.op == ">=" and key(version) < key(clause.version):
                return f"is older than {clause.version}"
        return None


VersionKey = Callable[[str], Any]


def parse_constraint(text: str | None, *, key: VersionKey = version_key) -> Constraint:
    """``">=metrics-v1,!=metrics-v3"`` as a :class:`Constraint`; ``None``/empty/``latest`` is
    the empty (newest-stored) constraint. Every version is validated by *key* now, so a typo
    fails before any query."""
    if text is None or not text.strip() or text.strip() == LATEST:
        return Constraint()
    clauses: list[Clause] = []
    for part in (p.strip() for p in text.split(",")):
        clauses.extend(_parse_clause(part, text, key))
    return Constraint(tuple(clauses))


def _parse_clause(part: str, text: str, key: VersionKey) -> list[Clause]:
    if not part:
        raise VersionError(f"empty clause in {text!r}")
    if part == LATEST:
        return []
    match = _CLAUSE_RE.match(part)
    if match is None:
        raise VersionError(f"cannot parse clause {part!r} in {text!r}")
    version = match.group("version")
    key(version)
    return [Clause(match.group("op") or "=", version)]


def pick_version(
    stored: Sequence[str], constraint: Constraint, *, key: VersionKey, what: str
) -> str:
    """The highest of *stored* (oldest-first) satisfying *constraint*; a :class:`VersionError`
    naming why each stored candidate was rejected when none does."""
    satisfying = [v for v in stored if constraint.rejection(v, key) is None]
    if satisfying:
        return satisfying[-1]
    if not stored:
        raise VersionError(f"no {what} is stored, so {str(constraint)!r} cannot be satisfied")
    reasons = "; ".join(f"{v} {constraint.rejection(v, key)}" for v in stored)
    raise VersionError(f"no stored {what} satisfies {str(constraint)!r}: {reasons}")


ALL_GROUPS: Final[str] = "*"


def _has_operator_syntax(text: str) -> bool:
    return any(tok in text for tok in (">=", "!=")) or any(
        part.strip() == LATEST or part.strip().endswith(f"={LATEST}") for part in text.split(",")
    )


def parse_metric_constraints(
    text: str | None,
) -> str | dict[str, str] | dict[str, Constraint] | None:
    """A ``--metrics-version`` text that may use constraint operators (T-093).

    Text without ``>=``/``!=``/``latest`` is handed to :func:`parse_metric_selection`
    unchanged, so every T-090 form parses -- and later resolves -- exactly as before. Otherwise
    each comma-separated part is ``[GROUP]OP VERSION``; a part without a group continues the
    group named before it, or applies to every group (key :data:`ALL_GROUPS`) when it comes
    first: ``valuation>=metrics-v1,!=metrics-v3`` or ``>=metrics-v2``."""
    if text is None or not text.strip() or not _has_operator_syntax(text):
        return parse_metric_selection(text)
    clauses: dict[str, list[Clause]] = {}
    current = ALL_GROUPS
    for raw in (p.strip() for p in text.split(",")):
        clause_text = raw
        group_match = _GROUP_CLAUSE_RE.match(raw)
        if group_match is not None:
            current = group_match.group("group")
            if current in clauses:
                raise VersionError(f"group {current!r} given more than once in {text!r}")
            rest = group_match.group("rest")
            clause_text = LATEST if rest == f"={LATEST}" else rest
            clauses[current] = []
        clauses.setdefault(current, []).extend(_parse_clause(clause_text, text, version_key))
    return {group: Constraint(tuple(cs)) for group, cs in clauses.items()}


def choose_constrained_versions(
    present: Mapping[str, Iterable[str]],
    selection: str | Mapping[str, str] | Mapping[str, Constraint] | None = None,
    *,
    groups: Sequence[str] = METRIC_GROUPS,
) -> MetricVersions:
    """:func:`choose_versions`, extended to :class:`Constraint` selections (pure).

    A T-090 selection (``None``, a version, ``GROUP=VERSION`` pairs) goes to
    :func:`choose_versions` unchanged. A constraint selection resolves each group the consumer
    reads to the highest stored version satisfying its constraint (the group's own, else the
    :data:`ALL_GROUPS` one); a group with no stored rows is skipped unless named explicitly."""
    if (
        selection is None
        or isinstance(selection, str)
        or not any(isinstance(v, Constraint) for v in selection.values())
    ):
        return choose_versions(present, selection, groups=groups)  # type: ignore[arg-type]
    named = [g for g in selection if g != ALL_GROUPS]
    unknown = [g for g in named if g not in METRIC_GROUPS]
    if unknown:
        raise VersionError(f"unknown metric group(s) {unknown}; known: {list(METRIC_GROUPS)}")
    unread = [g for g in named if g not in groups]
    if unread:
        raise VersionError(
            f"metric group(s) {unread} are not read by this consumer (it reads {list(groups)})"
        )
    resolved: dict[str, str] = {}
    for group in groups:
        stored = sort_versions(present.get(group, ()))
        constraint = cast(
            "Constraint", selection.get(group) or selection.get(ALL_GROUPS) or Constraint()
        )
        if not stored and group not in selection:
            continue  # nothing stored for this group, and the user did not ask for it by name
        resolved[group] = pick_version(
            stored, constraint, key=version_key, what=f"metrics version for group {group!r}"
        )
    everywhere = selection.get(ALL_GROUPS)
    if isinstance(everywhere, Constraint) and not everywhere.is_latest and groups and not resolved:
        raise VersionError(
            f"no group this consumer reads ({list(groups)}) stores a metrics version "
            f"satisfying {str(everywhere)!r}"
        )
    return MetricVersions(resolved)


def engine_version_key(family: str) -> VersionKey:
    """The ordering of one engine's versions, ``<family>-v<N>`` by ``N``: ``qret-v2`` <
    ``qret-v10``. A version of another family is an error, not a guess."""

    def key(version: str) -> int:
        match = _VERSION_RE.match(version)
        if match is None or match.group("family") != family:
            raise VersionError(f"{version!r} is not a {family} version: expected '{family}-v<N>'")
        return int(match.group("n"))

    return key


def recognized_engine_versions(stored: Iterable[str], family: str) -> tuple[list[str], list[str]]:
    """*stored* split into ``(recognized oldest-first, unrecognized)`` for one engine family.

    Unrecognized strings are history the consumer never reads (e.g. ``corpact-v1-derived``)."""
    key = engine_version_key(family)
    good, other = [], []
    for version in set(stored):
        try:
            key(version)
        except VersionError:
            other.append(version)
        else:
            good.append(version)
    return sorted(good, key=key), sorted(other)
