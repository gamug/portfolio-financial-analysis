"""D6 (T-153 part 3b): each leaver's SEC SIC code against its GICS label -- a check, never a source.

    uv run python scripts/leaver_sic_check.py [--types FILE] [--out FILE]

Read-only. For every company that left the S&P 500 inside the backtest window it reads the SIC code
from its SEC submissions file and compares it with the delivered GICS label on the four types the
GICS request names -- REIT (6798), utilities (49xx), insurers (63xx) and banks (602x). **No label is
changed because of the SIC**: the code is a cross-check only (checklist APP-00; the request,
``docs/upstream_requests/data_mining_leaver_gics.md``).

A disagreement is reported in both directions: the label names one of the four types and the SIC
does not, or the SIC names one and the label does not. The label's class comes from APP-00's own
maps (``ARTICLE_7_CODES``'s names, ``BANK_SUB_INDUSTRIES``, the REIT predicate, the Utilities
sector); the SIC's from the code's band. Each disagreement is opened by hand in the PR and in
``docs/model_fixes.md``.

Output is committed (``docs/leaver_sic_check.json``) so the check is reproducible from the repo.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sec_xcheck.common import SecClient, cache_dir, cik10  # noqa: E402
from sec_xcheck.company_types import (  # noqa: E402
    BANK_SUB_INDUSTRIES,
    INSURER_SUB_INDUSTRIES,
    is_reit,
)

DEFAULT_TYPES = REPO / "docs" / "leaver_company_types.csv"
DEFAULT_OUT = REPO / "docs" / "leaver_sic_check.json"
REIT_SIC = "6798"


def label_class(sector: str, sub_industry: str) -> str | None:
    """The one of D6's four types the GICS label names, or ``None``."""
    if is_reit(sector, sub_industry):
        return "REIT"
    if sector == "Utilities":
        return "utility"
    if sub_industry in INSURER_SUB_INDUSTRIES:
        return "insurer"
    if sub_industry in BANK_SUB_INDUSTRIES:
        return "bank"
    return None


def sic_class(sic: Any) -> str | None:
    """The one of D6's four types the SIC code names, or ``None``."""
    code = str(sic or "").strip()
    if not code:
        return None
    if code == REIT_SIC:
        return "REIT"
    if code.startswith("49"):
        return "utility"
    if code.startswith("63"):
        return "insurer"
    if code.startswith("602"):
        return "bank"
    return None


def check(types_path: str, client: Any) -> dict[str, Any]:
    """The whole check document: one row per leaver CIK and the disagreements."""
    text = Path(types_path).read_text(encoding="utf-8")
    rows = list(
        csv.DictReader(
            io.StringIO(
                "\n".join(line for line in text.splitlines() if line and not line.startswith("#"))
            )
        )
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        doc = client.get("submissions", cik10(row["cik"])) or {}
        sic, described = doc.get("sic"), doc.get("sicDescription")
        from_label, from_sic = (
            label_class(row["gics_sector"], row["gics_sub_industry"]),
            sic_class(sic),
        )
        checked = bool(from_label or from_sic)
        out.append(
            {
                "cik": row["cik"],
                "symbols": row["symbols"],
                "gics_sector": row["gics_sector"],
                "gics_sub_industry": row["gics_sub_industry"],
                "label_class": from_label or "",
                "sic": str(sic or ""),
                "sic_description": str(described or ""),
                "sic_class": from_sic or "",
                "checked": checked,
                "agrees": (not checked) or from_label == from_sic,
            }
        )
    disagreements = [r for r in out if not r["agrees"]]
    return {
        "types": str(types_path),
        "leavers": len(out),
        "checked": sum(1 for r in out if r["checked"]),
        "disagreements": len(disagreements),
        "rows": out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--types", default=str(DEFAULT_TYPES), help="the staged leaver evidence")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()

    client = SecClient(cache_dir(), offline=True)
    report = check(args.types, client)
    Path(args.out).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(
        f"{report['leavers']} leavers: {report['checked']} carry one of D6's four types, "
        f"{report['disagreements']} disagreements; wrote {args.out}"
    )
    for row in report["rows"]:
        if row["checked"]:
            flag = "ok      " if row["agrees"] else "DISAGREE"
            print(
                f"  {flag} {row['symbols']:14} label {row['label_class'] or '-':8}"
                f" sic {row['sic'] or '-':5} {row['sic_class'] or '-':8} {row['sic_description']}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
