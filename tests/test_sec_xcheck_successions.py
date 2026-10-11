"""``scripts/sec_xcheck/successions.py``: the register of succession 8-Ks (APP-10b-1, T-149).

The gateway cannot list ``8-K12B`` / ``8-K12G3`` / ``8-K15D5`` (its ``form`` is an enum of four
and ``8-K`` returns plain 8-Ks -- checked live against APO, BLK, APTV and BG on 2026-10-11), so the
register reads the SEC's submissions. The rows are Apollo's and BlackRock's real filings."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from portfolio_common.db import Database
from sec_xcheck import successions
from sec_xcheck.common import SecClient


def _submissions(cache: Path, cik: str, rows: list[tuple[str, str, str]]) -> None:
    (cache / "submissions").mkdir(parents=True, exist_ok=True)
    recent = {
        "accessionNumber": [r[0] for r in rows],
        "form": [r[1] for r in rows],
        "filingDate": [r[2] for r in rows],
        "reportDate": [""] * len(rows),
        "acceptanceDateTime": [""] * len(rows),
        "primaryDocument": [""] * len(rows),
    }
    doc = {"cik": cik, "filings": {"recent": recent, "files": []}}
    (cache / "submissions" / f"CIK{cik}.json").write_text(json.dumps(doc))


def test_the_register_lists_succession_8ks_and_links_nothing(
    memory_db: Database, tmp_path: Path
) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'APO', '0001858681')")
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (2, 'BLK', '0002012383')")
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (3, 'AAA', '0000000003')")
    _submissions(
        tmp_path,
        "0001858681",
        [
            ("0001193125-22-000274", "8-K12B", "2022-01-03"),
            ("0001858681-22-000049", "8-K", "2022-11-02"),
            ("0001858681-21-000001", "8-K12B", "2021-06-01"),  # before our filings start
        ],
    )
    _submissions(
        tmp_path,
        "0002012383",
        [("0001193125-24-229601", "8-K12B", "2024-10-01"), ("x-1", "10-K", "2025-02-01")],
    )
    _submissions(tmp_path, "0000000003", [("x-2", "8-K", "2023-01-01")])
    rows = successions.build_register(memory_db, SecClient(tmp_path, offline=True))
    assert [(r["ticker"], r["form"], r["filing_date"], r["accession_number"]) for r in rows] == [
        ("APO", "8-K12B", "2022-01-03", "0001193125-22-000274"),
        ("BLK", "8-K12B", "2024-10-01", "0001193125-24-229601"),
    ]
    assert all(r["linked_to"] == "" for r in rows)  # linking needs a hand reading (APP-10b-2)


def test_an_amended_succession_is_registered_too(memory_db: Database, tmp_path: Path) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'BG', '0001996862')")
    _submissions(tmp_path, "0001996862", [("a-1", "8-K12G3/A", "2023-12-01")])
    (row,) = successions.build_register(memory_db, SecClient(tmp_path, offline=True))
    assert row["form"] == "8-K12G3/A"


def test_the_csv_carries_the_documented_columns(memory_db: Database, tmp_path: Path) -> None:
    memory_db.execute("INSERT INTO assets (id, ticker, cik) VALUES (1, 'APO', '0001858681')")
    _submissions(tmp_path, "0001858681", [("0001193125-22-000274", "8-K12B", "2022-01-03")])
    rows = successions.build_register(memory_db, SecClient(tmp_path, offline=True))
    out = tmp_path / "successions.csv"
    successions.write_csv(rows, out)
    with out.open(encoding="utf-8") as fh:
        read = list(csv.DictReader(fh))
    assert tuple(read[0]) == successions.FIELDS and read[0]["ticker"] == "APO"
