"""``python -m quant versions``: what there is to constrain (T-093).

For each input ``quant`` can be constrained on, the versions stored, their row counts and
their first/last write time, oldest first -- so a user choosing ``--metrics-version``,
``--returns-version``, ``--risk-model-version`` or ``--corpact-version`` (or writing a profile)
can see what each constraint would resolve against.
"""

from __future__ import annotations

from collections import defaultdict

from portfolio_common.db import Database

from kg_schema import queries
from kg_schema.versions import recognized_engine_versions, sort_versions
from quant.config import QuantSettings
from quant.db import VersionStat, version_stats
from quant.manifest import ENGINE_FAMILIES, QUANT_METRIC_GROUPS


def _line(stat: VersionStat, note: str = "") -> str:
    when = f"{stat.first_at or '?'} .. {stat.last_at or '?'}"
    return f"    {stat.version:<22} {stat.n_rows:>10,} rows   {when}" + (
        f"   {note}" if note else ""
    )


def _metrics_section(conn: Database) -> list[str]:
    stats = queries.metric_version_stats(conn)
    lines = [f"metrics (--metrics-version; quant reads: {', '.join(QUANT_METRIC_GROUPS)})"]
    for group in QUANT_METRIC_GROUPS:
        by_version = {
            version: VersionStat(version, n, first, last)
            for g, version, n, first, last in stats
            if g == group
        }
        if not by_version:
            lines.append(f"  {group}: nothing stored")
            continue
        ordered = sort_versions(by_version)
        lines.append(f"  {group}:")
        lines.extend(
            _line(by_version[v], "newest (default)" if v == ordered[-1] else "") for v in ordered
        )
    return lines


def _engine_section(
    conn: Database, input_name: str, flag: str, default_note: str, default_version: str | None
) -> list[str]:
    stats = {s.version: s for s in version_stats(conn, input_name)}
    recognized, other = recognized_engine_versions(stats, ENGINE_FAMILIES[input_name])
    lines = [f"{input_name.replace('_', ' ')} ({flag}; default: {default_note})"]
    if not stats:
        return [*lines, "    nothing stored"]
    for v in recognized:
        notes = []
        if v == recognized[-1]:
            notes.append("newest")
        if v == default_version:
            notes.append("configured")
        lines.append(_line(stats[v], ", ".join(notes)))
    lines.extend(_line(stats[v], "not a version quant reads (history)") for v in other)
    return lines


def _risk_model_section(conn: Database, settings: QuantSettings) -> list[str]:
    grouped: dict[str, list[VersionStat]] = defaultdict(list)
    for s in version_stats(conn, "risk_model"):
        grouped[s.version.split("+", 1)[0]].append(s)
    lines = [
        "risk model (--risk-model-version, optimize; default: --model-version, "
        f"{settings.risk_model_version})"
    ]
    if not grouped:
        return [*lines, "    nothing stored"]
    recognized, other = recognized_engine_versions(grouped, ENGINE_FAMILIES["risk_model"])
    for label in [*recognized, *other]:
        rows = grouped[label]
        firsts = [r.first_at for r in rows if r.first_at]
        lasts = [r.last_at for r in rows if r.last_at]
        combined = VersionStat(
            label, sum(r.n_rows for r in rows), min(firsts, default=None), max(lasts, default=None)
        )
        note = f"{len(rows)} manifest(s)"
        if label == settings.risk_model_version:
            note += ", configured"
        lines.append(_line(combined, note).replace(" rows ", " models "))
    return lines


def versions_report(conn: Database, settings: QuantSettings) -> str:
    """The whole ``quant versions`` report as text."""
    sections = [
        _metrics_section(conn),
        _engine_section(
            conn,
            "returns",
            "--returns-version, build-risk-model / optimize",
            f"the configured {settings.return_engine_version}",
            settings.return_engine_version,
        ),
        _risk_model_section(conn, settings),
        _engine_section(
            conn,
            "corpact",
            "--corpact-version, build-returns",
            "per asset, the newest gateway engine it has",
            None,
        ),
    ]
    return "\n\n".join("\n".join(s) for s in sections)
