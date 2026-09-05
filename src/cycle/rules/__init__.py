"""Rule registry + catalog seeding."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from portfolio_common.db import Database

from cycle.rules.base import Rule, RuleContext, VetoHit
from cycle.rules.builtin import RULES

__all__ = ["RULES", "Rule", "RuleContext", "VetoHit", "enabled_rules", "seed_catalog"]


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
