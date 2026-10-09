"""APP-00: the company-type table, one row per CIK, every assignment with its evidence.

Types (checklist v1.2.2, APP-00): ``article_9`` (a bank, bank holding or savings-and-loan holding company, by the
10-K's own statement), ``article_7`` (an insurer: GICS 40301020/30/40/50 plus the statement form), ``other_financial``
(Article 5 financial firms), ``multi_sector_holding``, ``operating``. Overlays: ``reit`` (GICS 6010 equity REITs),
``negative_equity`` (latest stored filing), ``multi_class`` (the SEC lists more than one ticker for the CIK).

GICS: the universe carries sub-industry *names* (the live snapshot, L-03); :data:`GICS_FINANCIAL_CODES` maps the
financial ones to the codes the checklist's table uses. The Article 9 evidence is a sentence of the latest 10-K naming
the filer a bank, financial or savings-and-loan holding company, with its accession; a firm for which no 10-K sentence
is found keeps its GICS-based type and is listed for the ticker-override format when the doubt matters.
"""

from __future__ import annotations

import re
from typing import Any

from sec_xcheck.common import cik_family
from sec_xcheck.submissions import all_filings

# sub-industry name -> GICS code (2023 structure), for the financial sub-industries of APP-00's table.
GICS_FINANCIAL_CODES: dict[str, str] = {
    "Diversified Banks": "40101010",
    "Regional Banks": "40101015",
    "Diversified Financial Services": "40201020",
    "Multi-Sector Holdings": "40201030",
    "Specialized Finance": "40201040",
    "Commercial & Residential Mortgage Finance": "40201050",
    "Transaction & Payment Processing Services": "40201060",
    "Consumer Finance": "40202010",
    "Asset Management & Custody Banks": "40203010",
    "Investment Banking & Brokerage": "40203020",
    "Diversified Capital Markets": "40203030",
    "Financial Exchanges & Data": "40203040",
    "Mortgage REITs": "40204010",
    "Insurance Brokers": "40301010",
    "Life & Health Insurance": "40301020",
    "Multi-line Insurance": "40301030",
    "Property & Casualty Insurance": "40301040",
    "Reinsurance": "40301050",
}
ARTICLE_7_CODES = {"40301020", "40301030", "40301040", "40301050"}
OTHER_FINANCIAL_CODES = {
    "40201020", "40201040", "40201050", "40202010", "40203010", "40203020", "40203030", "40204010",
}  # fmt: skip
BANK_CODES = {"40101010", "40101015"}
OPERATING_FINANCIAL_CODES = {
    "40201060",
    "40203040",
    "40301010",
}  # payments, exchanges & data, insurance brokers

HOLDING_PHRASE = re.compile(
    r"\b(?:(?:bank|financial|savings and loan|savings) holding compan(?:y|ies)|BHCs?|FHCs?|SLHCs?)\b",
    re.IGNORECASE,
)
# the filer speaks about itself: "we are", "is a", "as a", "became a", "registered as", "elected to be treated as" ...
SELF_STATEMENT = re.compile(
    r"\b(?:we are|we became|is a|is an|is also a|are a|as a|became a|registered as|elected to (?:be treated as|become)|"
    r"qualif\w+ as|operates as|treated as)\b",
    re.IGNORECASE,
)
# sentences about somebody else (an acquired company, a glossary entry, an officer's former employer) are not evidence
NOT_THE_FILER = re.compile(
    r"\b(?:acquisition of|acquired|served as|general counsel|merger with|definition|glossary|abbreviations?)\b",
    re.IGNORECASE,
)
NEGATED = re.compile(
    r"\b(?:not|nor|neither|never)\b[^.]{0,40}(?:holding compan|\bBHC\b|\bFHC\b)", re.IGNORECASE
)
GENERIC_SUBJECT = re.compile(
    r"^\W*(?:a|an|any|each|every|the)\s+(?:bank|financial|savings and loan)?\s*holding compan|\bBHCs? (?:that|which)\b|"
    r"\bat least one-half\b|\bsource of (?:financial|strength)\b",
    re.IGNORECASE,
)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"\u201c(])")
MAX_SENTENCE = 600
PREMIUM_CONCEPTS = ("us-gaap_PremiumsEarnedNet", "us-gaap_PremiumsWrittenNet", "us-gaap_Revenues")


def strip_html(html: str) -> str:
    """Visible text of an HTML document, whitespace collapsed (inline XBRL tags included as text)."""
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"&#160;|&nbsp;", " ", text)
    text = re.sub(r"&#8217;|&rsquo;", "'", text)
    return re.sub(r"\s+", " ", text)


def statement_score(sentence: str) -> int:
    """How well a sentence states the filer's own status; 0 when it does not (generic, negated, about another company)."""
    if not HOLDING_PHRASE.search(sentence) or not SELF_STATEMENT.search(sentence):
        return 0
    if (
        NOT_THE_FILER.search(sentence)
        or NEGATED.search(sentence)
        or GENERIC_SUBJECT.search(sentence)
    ):
        return 0
    direct = re.search(
        r"\b(?:we are|is a|is an|are a|became a|registered as|has elected to be treated as)\b[^.]{0,80}"
        r"(?:holding compan|\bBHC\b|\bFHC\b|\bSLHC\b)",
        sentence,
        re.IGNORECASE,
    )
    return 2 if direct else 1


def article_9_statement(text: str) -> str | None:
    """The best sentence in which the filer says it is a bank, financial or savings-and-loan holding company.

    A sentence counts when it names the status (or its abbreviation: BHC, FHC, SLHC), speaks about the filer itself
    (:data:`SELF_STATEMENT`), is not negated ("we are not a bank holding company") and is not about an acquired
    company, a glossary entry or holding companies in general. The first sentence of the highest score wins."""
    best: tuple[int, str] | None = None
    for sentence in SENTENCE_BREAK.split(text):
        if len(sentence) > MAX_SENTENCE:
            continue
        score = statement_score(sentence)
        if score and (best is None or score > best[0]):
            best = (score, sentence.strip()[:400])
    return best[1] if best else None


def classify(sub_industry: str, sector: str, *, article_9: bool) -> tuple[str, str]:
    """``(type, basis)`` per APP-00's table; *article_9* is the 10-K statement's presence."""
    if article_9:
        return (
            "article_9",
            "the 10-K states it is a bank, financial or savings-and-loan holding company",
        )
    code = GICS_FINANCIAL_CODES.get(sub_industry, "")
    by_code: list[tuple[bool, str, str]] = [
        (
            code in ARTICLE_7_CODES,
            "article_7",
            f"GICS {code}; statement form read from the stored statements",
        ),
        (code == "40201030", "multi_sector_holding", "GICS 40201030"),
        (
            code in OTHER_FINANCIAL_CODES,
            "other_financial",
            f"GICS {code}, no Article 9 statement found in the 10-K",
        ),
        (
            code in OPERATING_FINANCIAL_CODES,
            "operating",
            f"GICS {code} is an operating company under APP-00",
        ),
        (
            code in BANK_CODES,
            "other_financial",
            f"GICS {code} (bank) but no Article 9 statement found: check by hand",
        ),
    ]
    for matched, kind, basis in by_code:
        if matched:
            return kind, basis
    return "operating", "GICS sector " + (sector or "unknown")


def is_reit(sector: str, sub_industry: str) -> bool:
    return sector == "Real Estate" and sub_industry.endswith("REITs")


def premium_statement_form(facts: set[str]) -> bool:
    """An insurer's statement form: premiums earned on the face and no classified balance sheet."""
    return "us-gaap_PremiumsEarnedNet" in facts and "us-gaap_AssetsCurrent" not in facts


Row = dict[str, Any]


FIELDS = [
    "cik", "tickers", "company_name", "gics_sector", "gics_sub_industry", "gics_code", "type", "financial_firm",
    "basis", "evidence_accession", "evidence_quote", "reit", "negative_equity_latest", "multi_class_listed",
    "needs_override",
]  # fmt: skip
FINANCIAL_TYPES = {"article_9", "article_7", "other_financial"}


def latest_10k(filings_: list[dict[str, Any]]) -> dict[str, Any] | None:
    ks = sorted(
        (f for f in filings_ if f["form"] == "10-K"),
        key=lambda f: (f["filingDate"], f["accessionNumber"]),
    )
    return ks[-1] if ks else None


def article_9_evidence(client: Any, cik: str, filing: dict[str, Any]) -> str | None:
    """The Article 9 statement of a 10-K: its primary document first, then the filing's other body documents (a
    10-K that incorporates its business section from a second document, as BNY's does)."""
    primary = client.get_document(cik, filing["accessionNumber"], filing["primaryDocument"])
    found = article_9_statement(strip_html(primary)) if primary else None
    if found:
        return found
    for _name, html in client.get_filing_documents(
        cik, filing["accessionNumber"], filing["primaryDocument"]
    ):
        found = article_9_statement(strip_html(html))
        if found:
            return found
    return None


def insurer_form_evidence(facts: set[str]) -> str:
    return (
        "income statement carries PremiumsEarnedNet and the balance sheet no AssetsCurrent"
        if premium_statement_form(facts)
        else "statement form not confirmed from the stored facts (no PremiumsEarnedNet, or a classified balance sheet)"
    )


def build_rows(
    conn: Any,
    client: Any,
    equity_by_cik: dict[str, float] | None = None,
    *,
    check_documents: bool = True,
) -> list[Row]:
    """One row per CIK; Article 9 evidence is read from the latest 10-K of each non-insurer financial firm."""
    sectors = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM sectors")}
    groups: dict[str, list[Any]] = {}
    for a in conn.execute(
        "SELECT id, ticker, company_name, cik, sector_id, sub_industry FROM assets ORDER BY ticker"
    ):
        groups.setdefault(a["cik"], []).append(a)
    rows: list[Row] = []
    for cik, assets in groups.items():
        a = assets[0]
        sector, sub = sectors.get(a["sector_id"], ""), a["sub_industry"] or ""
        code = GICS_FINANCIAL_CODES.get(sub, "")
        filings_ = [
            f for c in cik_family(cik) for f in (all_filings(client, c, "2021-12-01") or [])
        ]
        latest = latest_10k(filings_)
        evidence = quote = None
        if check_documents and latest and sector == "Financials" and code not in ARTICLE_7_CODES:
            quote = article_9_evidence(client, cik, latest)
            evidence = latest["accessionNumber"] if quote else None
        kind, basis = classify(sub, sector, article_9=quote is not None)
        if kind == "article_7" and latest:
            fid = conn.execute(
                "SELECT id FROM sec_filings WHERE asset_id = ? AND accession_number = ?",
                (a["id"], latest["accessionNumber"]),
            ).fetchone()
            facts = (
                {
                    r["concept"]
                    for r in conn.execute(
                        "SELECT DISTINCT concept FROM financial_facts WHERE filing_id = ?",
                        (fid["id"],),
                    )
                }
                if fid
                else set()
            )
            basis = f"GICS {code}; {insurer_form_evidence(facts)}"
            evidence = latest["accessionNumber"]
        submissions_doc = client.get("submissions", cik)
        tickers = (submissions_doc or {}).get("tickers", [])
        equity = (equity_by_cik or {}).get(cik)
        negative = "" if equity is None else str(equity <= 0.0)
        override = kind == "other_financial" and code in BANK_CODES
        rows.append(
            {
                "cik": cik,
                "tickers": "/".join(x["ticker"] for x in assets),
                "company_name": a["company_name"],
                "gics_sector": sector,
                "gics_sub_industry": sub,
                "gics_code": code,
                "type": kind,
                "financial_firm": kind in FINANCIAL_TYPES,
                "basis": basis,
                "evidence_accession": evidence or "",
                "evidence_quote": (quote or "").replace("\n", " "),
                "reit": is_reit(sector, sub),
                "negative_equity_latest": negative,
                "multi_class_listed": len(tickers) > 1,
                "needs_override": override,
            }
        )
    return rows
