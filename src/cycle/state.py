"""``cycle_run`` / ``cycle_checkpoint`` bookkeeping.

This -- not the orchestration framework -- is the source of truth for resumability:
a step whose checkpoint is ``done`` for a given ``cycle_run`` is skipped on re-run.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from portfolio_common.db import Database

# Field names whose values must never reach params_json / detail_json. The cycle
# settings carry LLM_API_KEY, and json.dumps would otherwise persist it in the
# shared KG_FINANCIAL_DB that other repos read.
_SECRET_KEY_RE = re.compile(r"api[_-]?key|secret|token|password|passwd|credential", re.IGNORECASE)
_REDACTED = "***REDACTED***"


def _redact(value: Any) -> Any:
    """Deep-copy *value*, masking any dict entry whose key looks like a secret."""
    if isinstance(value, dict):
        return {
            key: (_REDACTED if val is not None and _SECRET_KEY_RE.search(key) else _redact(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


class ManifestMismatch(RuntimeError):
    """A ``cycle_run`` for this (type, date) exists, built on different input versions."""


def _manifest_fields(recorded: object, current: object) -> list[str]:
    """The manifest keys a run's record and this run's disagree on (T-153 part 3c, decision 1b)."""
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return []
    return [
        key for key in sorted(set(recorded) | set(current)) if recorded.get(key) != current.get(key)
    ]


def check_manifest(
    conn: Database,
    cycle_type: str,
    cycle_date: str,
    tag: str,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Refuse to resume an existing ``(cycle_type, cycle_date)`` run under a different manifest.

    ``cycle_run`` is unique per (type, date) and its outputs are keyed by date, so a second
    run over other input versions cannot sit beside the first (unlike ``quant``'s books) --
    resuming it would mix the two. A run recorded before T-090 has no manifest and is left
    alone. Call this *before* :func:`open_cycle`, which flips an existing run to ``running``.

    *manifest*, when given, lets the refusal name the field that differs: T-153 part 3c adds
    ``eligibility`` to the manifest, and a pre-3c run cannot be re-run under the new rule (its
    ranking would mix two eligibilities) -- it must be redone fresh or at another date."""
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    if row is None or not row["params_json"]:
        return
    try:
        recorded = json.loads(row["params_json"]).get("manifest_tag")
    except (TypeError, ValueError):
        return
    if recorded is not None and recorded != tag:
        fields = _manifest_fields(json.loads(row["params_json"]).get("manifest"), manifest)
        named = (
            f"the manifest field{'s' if len(fields) != 1 else ''} that differ"
            f"{'s' if len(fields) == 1 else ''}: {', '.join(fields)}; "
            if fields
            else ""
        )
        raise ManifestMismatch(
            f"the {cycle_type} run for {cycle_date} was built on manifest {recorded}; this run "
            f"resolves manifest {tag}. Resuming would mix inputs -- {named}a run whose manifest "
            f"names another eligibility (or none, before T-153 part 3c) cannot be re-run: redo it "
            f"fresh or at another date; any other difference needs the recorded input versions "
            f"(--metrics-version)"
        )


class ConstructionMismatch(RuntimeError):
    """A ``cycle_run`` for this (type, date) recorded other book-construction settings."""


def check_construction(
    conn: Database, cycle_type: str, cycle_date: str, construction: dict[str, Any]
) -> None:
    """Refuse to resume an existing run under different book-construction settings (T-136).

    The first attempt that reaches the ``positions`` step records its settings (N, scheme, caps,
    preferences) under ``params_json.construction``; resuming with others would build a second book
    beside the first and silently mix them, the same hazard :func:`check_manifest` refuses for input
    versions. A run with nothing recorded yet (it never reached ``positions``, or predates T-136) is
    left alone. Call this *before* :func:`open_cycle`."""
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    if row is None or not row["params_json"]:
        return
    try:
        recorded = json.loads(row["params_json"]).get("construction")
    except (TypeError, ValueError):
        return
    if not isinstance(recorded, dict) or recorded == construction:
        return
    changed = sorted(
        k for k in set(recorded) | set(construction) if recorded.get(k) != construction.get(k)
    )
    detail = "; ".join(f"{k}: {recorded.get(k)!r} -> {construction.get(k)!r}" for k in changed)
    raise ConstructionMismatch(
        f"the {cycle_type} run for {cycle_date} already built its book with other construction "
        f"settings ({detail}). Resuming would mix two books -- rerun with the recorded settings, "
        "or a different date"
    )


class ScoreWeightsMismatch(RuntimeError):
    """A ``cycle_run`` for this (type, date) recorded other composite score weights."""


def check_score_weights(
    conn: Database, cycle_type: str, cycle_date: str, score_weights: dict[str, float]
) -> None:
    """Refuse to resume an existing run under different composite score weights (T-141).

    ``open_cycle`` records ``params_json.score_weights`` once, when the run is first created, and a
    resume never rewrites it -- it is what ``v_weight_scheme``, ``v_weight_component`` and
    ``v_cycle_ranking_component`` report. Resuming under other weights would rank (or keep a ranking
    built) with weights the run does not record, so the read contract would disagree with the
    blend, the same hazard :func:`check_construction` refuses for the book. A run that recorded no
    weights is left alone. Call this *before* :func:`open_cycle`."""
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    if row is None or not row["params_json"]:
        return
    try:
        recorded = json.loads(row["params_json"]).get("score_weights")
    except (TypeError, ValueError):
        return
    if not isinstance(recorded, dict) or recorded == score_weights:
        return
    changed = sorted(
        k for k in set(recorded) | set(score_weights) if recorded.get(k) != score_weights.get(k)
    )
    detail = "; ".join(f"{k}: {recorded.get(k)!r} -> {score_weights.get(k)!r}" for k in changed)
    raise ScoreWeightsMismatch(
        f"the {cycle_type} run for {cycle_date} was ranked with other score weights ({detail}). "
        "Resuming would mix two blends -- rerun with the recorded weights, or a different date"
    )


class ReplayInputsMismatch(RuntimeError):
    """A ``cycle_run`` for this (type, date) recorded another selection cut (WP-C)."""


def check_replay_inputs(
    conn: Database,
    cycle_type: str,
    cycle_date: str,
    *,
    risk_model_id: int | None,
    veto_cutoff_same_day: bool,
) -> None:
    """Refuse to resume an existing run under another selection cut (WP-C).

    A REPLAY run is unique per (type, date) and ``rank``/``positions`` write its ranking and its
    book in place, so a second run at the same date under a different ``risk_model_id`` (the
    Top-N cohort restricted to a model's panel) or ``veto_cutoff_same_day`` (the T-1 contagion lag
    or not) would silently replace the first run's cohort. ``open_cycle`` records both once, when
    the run is first created, and a resume never rewrites them -- the same hazard
    :func:`check_manifest` refuses for input versions and :func:`check_score_weights` for the
    blend. A run recorded before this change has neither key; its behaviour was exactly
    ``risk_model_id=None`` with the T-1 cutoff, so that is what an absent key means here. Call
    this *before* :func:`open_cycle`, which flips an existing run to ``running``."""
    row = conn.execute(
        "SELECT params_json FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    if row is None or not row["params_json"]:
        return
    try:
        recorded = json.loads(row["params_json"])
    except (TypeError, ValueError):
        return
    if not isinstance(recorded, dict):
        return
    was_model = recorded.get("risk_model_id")
    was_cutoff = bool(recorded.get("veto_cutoff_same_day", False))
    if was_model == risk_model_id and was_cutoff == bool(veto_cutoff_same_day):
        return
    raise ReplayInputsMismatch(
        f"the {cycle_type} run for {cycle_date} selected under risk_model_id = {was_model!r} "
        f"and veto_cutoff_same_day = {was_cutoff!r}; this run asks for "
        f"risk_model_id = {risk_model_id!r} and veto_cutoff_same_day = "
        f"{bool(veto_cutoff_same_day)!r}. Resuming would replace that run's cohort -- rerun with "
        "the recorded cut, or reset the run (a different date, or `cycle backfill --force`)"
    )


def merge_params(conn: Database, cycle_run_id: int, updates: dict[str, Any]) -> None:
    """Merge *updates* into ``cycle_run.params_json`` (top-level keys; secrets stay redacted)."""
    row = conn.execute("SELECT params_json FROM cycle_run WHERE id = ?", (cycle_run_id,)).fetchone()
    try:
        params = json.loads(row["params_json"]) if row and row["params_json"] else {}
    except (TypeError, ValueError):
        params = {}
    params.update(_redact(updates))
    conn.execute(
        "UPDATE cycle_run SET params_json = ? WHERE id = ?",
        (json.dumps(params, default=str), cycle_run_id),
    )
    conn.commit()


def open_cycle(
    conn: Database,
    cycle_type: str,
    cycle_date: str,
    params: dict[str, Any],
    *,
    code_version: str | None = None,
) -> int:
    """Create or resume the ``cycle_run`` row for ``(cycle_type, cycle_date)``."""
    conn.execute(
        """
        INSERT INTO cycle_run (cycle_type, cycle_date, started_at, status, params_json, code_version)
        VALUES (?, ?, ?, 'running', ?, ?)
        ON CONFLICT (cycle_type, cycle_date) DO UPDATE SET
            status = 'running', code_version = excluded.code_version
        """,
        (
            cycle_type,
            cycle_date,
            _now(),
            json.dumps(_redact(params), default=str),
            code_version,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM cycle_run WHERE cycle_type = ? AND cycle_date = ?",
        (cycle_type, cycle_date),
    ).fetchone()
    return int(row["id"])


def checkpoint(
    conn: Database,
    cycle_run_id: int,
    step: str,
    status: str,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO cycle_checkpoint (cycle_run_id, step, status, detail_json, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (cycle_run_id, step) DO UPDATE SET
            status = excluded.status, detail_json = excluded.detail_json,
            updated_at = excluded.updated_at
        """,
        (cycle_run_id, step, status, json.dumps(_redact(detail or {}), default=str), _now()),
    )
    conn.commit()


def done_steps(conn: Database, cycle_run_id: int) -> set[str]:
    return {
        str(r["step"])
        for r in conn.execute(
            "SELECT step FROM cycle_checkpoint WHERE cycle_run_id = ? AND status = 'done'",
            (cycle_run_id,),
        )
    }


def finish_cycle(conn: Database, cycle_run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE cycle_run SET status = ?, finished_at = ? WHERE id = ?",
        (status, _now(), cycle_run_id),
    )
    conn.commit()
