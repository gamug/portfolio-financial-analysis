"""The D-11 baseline export and the APP-00 company-type table (T-147): what is exported, that nothing is overwritten,
and how a 10-K's own words decide Article 9."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from portfolio_common.db import Database
from sec_xcheck import baseline, company_types


# -- baseline ---------------------------------------------------------------------------------------
def _seed(path: Path) -> Path:
    raw = sqlite3.connect(path)
    for table, cols in {
        "cycle_run": "id INTEGER PRIMARY KEY, cycle_date TEXT",
        "cycle_ranking": "id INTEGER PRIMARY KEY, asset_id INTEGER",
        "portfolio_position": "id INTEGER PRIMARY KEY, asset_id INTEGER",
        "score_snapshot": "id INTEGER PRIMARY KEY, score_type TEXT",
        "veto": "id INTEGER PRIMARY KEY, rule_id TEXT",
        "rule_catalog": "rule_id TEXT PRIMARY KEY, description TEXT",
        "fundamental_metrics": "id INTEGER PRIMARY KEY, inputs_json TEXT",
    }.items():
        raw.execute(f"CREATE TABLE {table} ({cols})")
    raw.execute("INSERT INTO cycle_run VALUES (1, '2026-09-22')")
    raw.execute("INSERT INTO fundamental_metrics VALUES (1, NULL)")
    raw.execute("INSERT INTO rule_catalog VALUES ('NEGATIVE_FCF', 'x, with a comma')")
    raw.commit()
    raw.close()
    return path


def _ro(path: Path) -> Database:
    return Database.connect(path, read_only=True)


def test_the_export_writes_every_table_with_its_row_count_and_digest(tmp_path: Path) -> None:
    db_path = _seed(tmp_path / "prod.db")
    out = tmp_path / "baseline"
    files = baseline.export_baseline(_ro(db_path), db_path, out)
    names = {name for name, _n, _sha in files}
    assert names == {f"{t}.csv" for t in baseline.EXPORTS} | {"PROVENANCE.txt"}
    rows = {name: n for name, n, _s in files}
    assert (
        rows["cycle_run.csv"] == 1
        and rows["veto.csv"] == 0
        and rows["fundamental_metrics.csv"] == 1
    )
    for name, _n, sha in files:
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == sha
    assert "sha1 before export" in (out / "PROVENANCE.txt").read_text()


def test_an_existing_baseline_is_never_overwritten(tmp_path: Path) -> None:
    db_path = _seed(tmp_path / "prod.db")
    out = tmp_path / "baseline"
    baseline.export_baseline(_ro(db_path), db_path, out)
    before = (out / "cycle_run.csv").read_bytes()
    with pytest.raises(FileExistsError, match="never overwritten"):
        baseline.export_baseline(_ro(db_path), db_path, out)
    assert (out / "cycle_run.csv").read_bytes() == before


def test_the_database_is_byte_identical_after_the_export(tmp_path: Path) -> None:
    db_path = _seed(tmp_path / "prod.db")
    digest = baseline.file_digest(db_path, "sha1")
    baseline.export_baseline(_ro(db_path), db_path, tmp_path / "baseline")
    assert baseline.file_digest(db_path, "sha1") == digest


def test_the_manifest_lists_files_rows_and_the_checksum(tmp_path: Path) -> None:
    text = baseline.manifest_markdown(
        [("cycle_run.csv", 2, "ab" * 32)],
        tmp_path / "prod.db",
        "5c5c",
        tmp_path / "b",
        "2026-10-08",
    )
    assert "| `cycle_run.csv` | 2 | `" in text and "`5c5c`" in text and "2026-10-08" in text


# -- company types -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sentence",
    [
        "JPMorgan Chase & Co. is a bank holding company (BHC) and a financial holding company (FHC) under U.S. federal law.",
        "We are a BHC incorporated under Delaware state law in 1984, and our primary federal regulator is the FRB.",
        "The Charles Schwab Corporation (CSC) is a savings and loan holding company.",
        "BNY is registered as an FHC under the BHC Act and subject to supervision by the Federal Reserve.",
    ],
)
def test_a_filer_that_says_it_is_a_holding_company_has_an_article_9_statement(
    sentence: str,
) -> None:
    assert company_types.article_9_statement(f"Intro text. {sentence} More text.") == sentence


@pytest.mark.parametrize(
    "sentence",
    [
        "Although we are not a bank holding company for purposes of United States law, we are regulated.",  # negated (COIN)
        "A bank holding company is also required by law to act as a source of financial and managerial strength.",  # generic
        "Acquisition of FirstBank Holding Company On January 5, 2026, PNC completed its acquisition.",  # somebody else
        "BHC Bank holding company BHCA Bank Holding Company Act of 1956, as amended.",  # a glossary entry
        "He served as General Counsel of Cullen/Frost Bankers, Inc., a financial holding company.",  # an officer's past
    ],
)
def test_sentences_that_are_not_the_filers_own_status_are_not_evidence(sentence: str) -> None:
    assert company_types.article_9_statement(f"Intro. {sentence} Tail.") is None


def test_the_best_statement_wins_over_the_first_mention() -> None:
    text = "Federal rules govern a BHC's capital. Citigroup is a registered bank holding company and financial holding company."
    assert (
        company_types.article_9_statement(text)
        == "Citigroup is a registered bank holding company and financial holding company."
    )


def test_types_follow_app_00() -> None:
    c = company_types.classify
    assert c("Regional Banks", "Financials", article_9=True)[0] == "article_9"
    assert c("Property & Casualty Insurance", "Financials", article_9=False)[0] == "article_7"
    assert (
        c("Asset Management & Custody Banks", "Financials", article_9=False)[0] == "other_financial"
    )
    assert (
        c("Transaction & Payment Processing Services", "Financials", article_9=False)[0]
        == "operating"
    )
    assert c("Financial Exchanges & Data", "Financials", article_9=False)[0] == "operating"
    assert c("Insurance Brokers", "Financials", article_9=False)[0] == "operating"
    assert c("Multi-Sector Holdings", "Financials", article_9=False)[0] == "multi_sector_holding"
    assert c("Machinery", "Industrials", article_9=False)[0] == "operating"
    assert (
        c("Regional Banks", "Financials", article_9=False)[0] == "other_financial"
    )  # a bank with no statement: by hand


def test_reit_overlay_is_gics_equity_reits() -> None:
    assert company_types.is_reit("Real Estate", "Retail REITs")
    assert not company_types.is_reit("Real Estate", "Real Estate Services")
    assert not company_types.is_reit("Financials", "Mortgage REITs")  # financials, not 6010


def test_an_insurers_statement_form_is_premiums_without_a_classified_balance_sheet() -> None:
    assert company_types.premium_statement_form({"us-gaap_PremiumsEarnedNet", "us-gaap_Assets"})
    assert not company_types.premium_statement_form(
        {"us-gaap_PremiumsEarnedNet", "us-gaap_AssetsCurrent"}
    )


def test_strip_html_keeps_the_visible_text() -> None:
    html = "<html><style>p{}</style><body><p>We&#160;are&#8217;re <b>a bank</b> holding company.</p><script>x()</script></body></html>"
    assert "bank holding company" in company_types.strip_html(
        html
    ) and "x()" not in company_types.strip_html(html)
