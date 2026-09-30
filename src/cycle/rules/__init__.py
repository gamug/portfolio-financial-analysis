"""Rule registry + catalog seeding."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from portfolio_common.db import Database

from cycle.rules.base import Rule, RuleContext, RuleResult, VetoHit
from cycle.rules.builtin import RULES

__all__ = [
    "RULES",
    "Rule",
    "RuleContext",
    "RuleResult",
    "VetoHit",
    "disabled_rule_ids",
    "enabled_rules",
    "seed_catalog",
]


def seed_catalog(conn: Database) -> None:
    """Insert any missing rules into ``rule_catalog`` (never overwrites)."""
    now = datetime.now(tz=UTC).isoformat(timespec="seconds")
    conn.executemany(
        """
        INSERT OR IGNORE INTO rule_catalog (rule_id, description, severity, params_json,
                                            enabled, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
        """,
        [(r.RULE_ID, r.DESCRIPTION, r.SEVERITY, json.dumps(r.PARAMS), now) for r in RULES],
    )
    conn.commit()


def enabled_rules(conn: Database) -> list[Rule]:
    rows = {
        str(r["rule_id"])
        for r in conn.execute("SELECT rule_id FROM rule_catalog WHERE enabled = 1")
    }
    return [r for r in RULES if r.RULE_ID in rows]


def disabled_rule_ids(conn: Database) -> set[str]:
    """rule_ids turned off in ``rule_catalog`` (T-125 c) -- a disabled rule's own open veto
    stints must still close at the current cycle date, even though it never reaches
    :func:`enabled_rules` and so is never asked to evaluate anything this cycle."""
    return {
        str(r["rule_id"])
        for r in conn.execute("SELECT rule_id FROM rule_catalog WHERE enabled = 0")
    }
