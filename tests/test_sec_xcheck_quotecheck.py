"""``scripts/sec_xcheck/quotecheck.py``: every quote of a checklist is verified against its source file (T-147)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sec_xcheck import quotecheck

ROW = (
    '| ID-01 | A rule. | A1_Guide p.2 | "Filers should normally assign an appropriate label for a concept" | why | '
    "metrics | all | WARN | note |\n"
)
SOURCE = "Preamble.\n\nIn preference to declaring a new concept, filers should normally assign an appropriate label for a concept already defined.\n"


def _sources(tmp_path: Path, text: str = SOURCE) -> Path:
    docs = tmp_path / "sources"
    docs.mkdir()
    (docs / "A1_Guide.md").write_text(text, encoding="utf-8")
    return docs


def _checklist(tmp_path: Path, row: str = ROW) -> Path:
    path = tmp_path / "checklist.md"
    path.write_text("<!-- TOTALS -->\n\n---\n\n" + row, encoding="utf-8")
    return path


def test_a_verbatim_quote_is_verified(tmp_path: Path, capsys: object) -> None:
    code = quotecheck.main(
        [str(_checklist(tmp_path)), "--sources", str(_sources(tmp_path)), "--quiet"]
    )
    assert code == 0
    assert "quotes 1  verified 1  problems 0" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_a_quote_that_is_not_in_the_source_fails_the_run(tmp_path: Path) -> None:
    docs = _sources(tmp_path, "Something else entirely.")
    assert quotecheck.main([str(_checklist(tmp_path)), "--sources", str(docs), "--quiet"]) == 1


def test_matching_ignores_case_spacing_punctuation_and_ligatures() -> None:
    assert quotecheck.norm("Filers  SHOULD, normally") == quotecheck.norm("filers should normally")
    # "fi", "ff" and "fl" ligatures are dropped by PDF text extraction: "financial" arrives as "nancial"
    assert quotecheck.norm_lig("financial") == quotecheck.norm_lig("nancial")


def test_elided_quotes_are_found_piece_by_piece() -> None:
    sources: list[tuple[str, set[int]]] = [("x.md", set())]
    quotecheck._cache["x.md"] = ["alpha beta gamma delta epsilon zeta eta theta"]
    status, _ = quotecheck.find("alpha beta gamma delta … epsilon zeta eta theta", sources)
    assert status.startswith("OK")


def test_source_tokens_resolve_to_one_file(tmp_path: Path) -> None:
    docs = _sources(tmp_path)
    assert quotecheck.resolve("A1", docs).endswith("A1_Guide.md")
    with pytest.raises(SystemExit):
        quotecheck.resolve("Z9", docs)


def test_write_replaces_the_totals_block(tmp_path: Path) -> None:
    path = _checklist(tmp_path)
    quotecheck.main([str(path), "--sources", str(_sources(tmp_path)), "--write", "--quiet"])
    text = path.read_text(encoding="utf-8")
    assert (
        "| ID | 1 |" in text and "**Quoted fragments:** 1; verified against the files: 1." in text
    )


def test_rule_rows_are_found_by_layer_prefix() -> None:
    rows = list(quotecheck.rows(ROW + "| NOTARULE-01 | x |\n"))
    assert [rid for rid, _ in rows] == ["ID-01"]
